"""参考数据：交易日历 + 标的基础表 + 上市/退市日期。

这三样是"地基"：没有日历，回测的每根 K 线都错位；
没有 list_date/delist_date，样本里就混进未来股 + 幸存者偏差。

分两级：
- 快路径 sync_securities()：一次请求拿全市场 code/name/status
- 慢路径 sync_security_details()：逐只补 ipoDate/outDate，走 checkpoint 增量
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from lquant.core.db import reader
from lquant.core.types import now_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.catalog import SecurityRepo, TradeCalendarRepo

_CAL_START = date(1990, 12, 19)  # 上交所开市
_CAL_END = date(2035, 12, 31)


def sync_calendar(start: date | str = _CAL_START, end: date | str = _CAL_END) -> int:
    """交易日历。幂等，重跑直接覆盖。

    没有它，后面所有对齐（回测日历、因子窗口、复权基准日）全是错的。
    """
    from loguru import logger

    from lquant.data.providers import get_provider

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else date.fromisoformat(end)

    df = get_provider().trade_calendar(start_d, end_d)
    if not len(df):
        logger.warning("交易日历返回为空")
        return 0
    n = TradeCalendarRepo().upsert(df)
    logger.info(f"交易日历 {n} 天（开市 {int(df['is_open'].sum())} 天）{start_d}~{end_d}")
    return n


def sync_securities(day: date | None = None) -> int:
    """快路径：全市场标的清单。库里已有 list_date 要保住，不能被覆盖成 NULL。

    day 参数已废弃（provider 接口 securities() 不收日期），仅为兼容保留。
    """
    from loguru import logger

    from lquant.data.base import source_name
    from lquant.data.providers import get_provider

    provider = get_provider()
    df = provider.securities()
    if not len(df):
        logger.warning("标的清单返回为空")
        return 0
    df = _merge_existing_details(df).with_columns(
        # 血缘标实际服务源（fallback 切到辅源时不能硬编码主源名）
        source=pl.lit(source_name(provider)),
        updated_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
    )
    n = SecurityRepo().upsert(df)
    logger.info(f"标的清单 {n} 只（源 {source_name(provider)}）")
    return n


def _ak_delist_module():
    """akshare 退市接口入口（独立函数便于测试替身替换）。"""
    import akshare  # noqa: PLC0415

    return akshare


def sync_delisted() -> int:
    """退市股名单（akshare 沪深退市接口）。幂等 upsert。

    快路径 sync_securities 只拿在市标的，退市股没有入口 → full_backfill
    的「security 表没有退市股」前置校验必然失败。这里补上退市名单；
    入表后慢路径 sync_security_details 会自动逐只补齐缺失字段。
    """
    from loguru import logger

    ak = _ak_delist_module()
    frames = []
    for fetch, code_col, name_col, list_col, delist_col in (
        (ak.stock_info_sh_delist, "公司代码", "公司简称", "上市日期", "暂停上市日期"),
        (ak.stock_info_sz_delist, "证券代码", "证券简称", "上市日期", "终止上市日期"),
    ):
        try:
            df = _normalize_delist(fetch(), code_col, name_col, list_col, delist_col)
        except Exception as e:  # noqa: BLE001 - 单一交易所失败不拖垮另一家
            logger.warning(f"退市名单获取失败（{code_col}）: {e}")
            continue
        if len(df):
            frames.append(df)
    if not frames:
        logger.warning("退市名单返回为空")
        return 0
    df = pl.concat(frames).unique(subset=["symbol"], keep="first")
    df = _merge_existing_details(df, keep_existing=True).with_columns(
        source=pl.lit("akshare"),
        updated_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
    )
    n = SecurityRepo().upsert(df)
    logger.info(f"退市股名单 {n} 只")
    return n


def _normalize_delist(
    df, code_col: str, name_col: str, list_col: str, delist_col: str
) -> pl.DataFrame:
    """交易所退市表 → security 表列（symbol 统一 000003.SZ 形态）。

    B 股等 parse_symbol 不认识的代码跳过（不能让一只 200002 拖垮整张退市表）。
    """
    from loguru import logger

    from lquant.core.types import parse_symbol

    def _safe_symbol(raw: str) -> str | None:
        try:
            return str(parse_symbol(raw))
        except Exception:  # noqa: BLE001 - 无法识别的代码段（B股等），丢行
            return None

    if df is None or not len(df):
        return pl.DataFrame()
    out = pl.from_pandas(df) if not isinstance(df, pl.DataFrame) else df
    res = out.select(
        pl.col(code_col)
        .cast(pl.Utf8)
        .str.strip_chars()
        .map_elements(_safe_symbol, return_dtype=pl.Utf8)
        .alias("symbol"),
        pl.col(name_col).cast(pl.Utf8).alias("name"),
        pl.col(list_col).cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("list_date"),
        pl.col(delist_col).cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("delist_date"),
        pl.lit("stock").alias("sec_type"),
    )
    n_before = len(res)
    res = res.filter(pl.col("symbol").is_not_null())
    if skipped := n_before - len(res):
        logger.warning(f"退市名单跳过 {skipped} 只无法识别代码的标的（B股等）")
    return res


def sync_security_details(limit: int | None = None, batch: int = 200) -> int:
    """慢路径：逐只补 ipoDate / outDate，断点续传。

    5000 只 × 每只一次请求 ≈ 20-40 分钟，中途被杀是常态，所以每批写 checkpoint。
    """
    from loguru import logger

    from lquant.data.base import source_name
    from lquant.data.providers import get_provider

    repo = SecurityRepo()
    pending = repo.pending_details()
    if limit:
        pending = pending[:limit]
    if not pending:
        logger.info("没有待补详情的标的")
        return 0

    cp = Checkpoint("security_details")
    todo = cp.remaining(pending)
    logger.info(f"待补详情 {len(todo)} 只（已完成 {len(cp)}）")
    if not todo:
        return 0

    provider = get_provider()
    target = provider.providers[0] if hasattr(provider, "providers") else provider
    src = source_name(target)
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        df = target.security_details(chunk)
        if len(df):
            repo.upsert(
                df.with_columns(
                    source=pl.lit(src),
                    updated_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
                ).select(
                    [
                        c
                        for c in (
                            "symbol",
                            "name",
                            "list_date",
                            "delist_date",
                            "sec_type",
                            "source",
                            "updated_at",
                        )
                        if c in df.columns
                    ]
                )
            )
        cp.mark(chunk)
        done += len(chunk)
        logger.info(f"  详情进度 {done}/{len(todo)}")
    return done


def sync_reference(skip_details: bool = False, detail_limit: int | None = None) -> dict:
    """一键补齐地基。返回各步写入行数，供 CLI/脚本打印。

    单步失败不中断整体（日历挂了不能拖累退市名单入库），
    全部跑完后若有失败则汇总抛错 —— 已写入的部分不回滚，重跑幂等。
    """
    from loguru import logger

    out: dict = {}
    errors: list[str] = []
    steps: list[tuple[str, Callable[[], int]]] = [
        ("calendar", sync_calendar),
        ("securities", sync_securities),
        ("delisted", sync_delisted),
    ]
    if not skip_details:
        steps.append(("details", lambda: sync_security_details(limit=detail_limit)))
    for name, fn in steps:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - 单步失败记下，继续跑后面的步骤
            logger.warning(f"reference 步骤 {name} 失败: {e}")
            out[name] = 0
            errors.append(f"{name}: {e}")
    if errors:
        raise RuntimeError(
            "reference 同步部分失败（成功部分已写入，重跑幂等）: " + "; ".join(errors)
        )
    return out


def _merge_existing_details(df: pl.DataFrame, *, keep_existing: bool = False) -> pl.DataFrame:
    """保留库里已有的 list_date / delist_date，避免快路径把它们冲成 NULL。

    注意：INSERT OR REPLACE 是整行覆盖，不合并就会丢失慢路径的成果。
    keep_existing=True（退市名单等官方口径与库内已有值冲突时以库内为准）：
    旧值非空则完全保留，新值只补空缺。
    """
    try:
        with reader() as con:
            old = con.execute("SELECT symbol, list_date, delist_date FROM security").pl()
    except Exception:  # noqa: BLE001 - 表不存在等情况，直接跳过
        return df
    if not len(old):
        return df
    joined = df.join(old, on="symbol", how="left")
    # 新清单可能不带日期列（baostock 快路径只有 code/name/status）——
    # 此时 join 无列名冲突，不会产生 _right 后缀，旧值即最终值。
    exprs: dict[str, pl.Expr] = {}
    drops: list[str] = []
    for col in ("list_date", "delist_date"):
        if col in df.columns:
            new_c, old_c = (f"{col}_right", col) if keep_existing else (col, f"{col}_right")
            exprs[col] = pl.coalesce(
                pl.col(new_c).cast(pl.Date, strict=False),
                pl.col(old_c).cast(pl.Date, strict=False),
            )
            drops.append(f"{col}_right")
        else:
            exprs[col] = pl.col(col).cast(pl.Date, strict=False)
    out = joined.with_columns(**exprs)
    return out.drop(drops) if drops else out


__all__ = [
    "sync_calendar",
    "sync_securities",
    "sync_delisted",
    "sync_security_details",
    "sync_reference",
]
