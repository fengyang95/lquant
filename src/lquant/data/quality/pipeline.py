"""质量门禁：同步链路的统一入口。

写入湖之前跑记录级断言（fatal 阻断），落库后可对全湖跑
序列/截面级 validators（`lq data check`）。issue 一律落
data_quality_issue 表 —— 标记而非删除。
"""
from __future__ import annotations

from datetime import timedelta

import polars as pl

from lquant.core.errors import DataQualityError
from lquant.data.quality import asserts, validators
from lquant.data.quality.issues import Issue, save_issues

__all__ = ["gate_daily", "run_lake_checks"]

# 涨跌停检查只用当前 board/is_st 快照（PIT 局限），只跑近端窗口
_LIMIT_WINDOW_DAYS = 400


def gate_daily(df: pl.DataFrame, *, data_version: str | None = None,
               raise_on_fatal: bool = True) -> tuple[pl.DataFrame, list[Issue]]:
    """日线批次入湖前的门禁：八项断言（记录级）+ 打 quality_flags。

    fatal 失败抛 DataQualityError 阻断下游（§3.8.6）；
    warn 只打标 + 落 issue，批次照常落地。
    """
    out, found = asserts.run_record_checks(df, raise_on_fatal=False)
    try:
        save_issues(found, data_version=data_version)
    except Exception as e:  # noqa: BLE001  issue 落库失败不能阻断同步（打标已在 df 上）
        from loguru import logger
        logger.error(f"issue 落库失败（不阻断批次）: {e}")
    fatal = [i for i in found if i.severity == "fatal"]
    if fatal and raise_on_fatal:
        raise DataQualityError(fatal[0].rule.lower(), fatal[0].detail)
    return out, found


def run_lake_checks(start: str | None = None, end: str | None = None,
                    *, data_version: str | None = None) -> list[Issue]:
    """对全湖跑序列/截面级 validators，issue 落库并返回。

    涨跌停约束 / 覆盖度 / 僵尸 / 复权因子单调 / 日历对齐。
    参考表（security / trade_calendar）缺失的检查自动跳过 ——
    质量检查自身不能成为同步链路的单点故障。
    """
    from lquant.data.store.parquet import read_daily

    df = read_daily(start=start, end=end).collect()
    if not len(df):
        return []

    # 只挑检查需要的列 —— 全湖宽表全量物化是纯浪费（M5）
    keep = [c for c in ("symbol", "trade_date", "open", "close", "pre_close",
                        "volume", "adj_factor") if c in df.columns]
    df = df.select(keep)

    if data_version is None:
        from lquant.data import lineage
        data_version = lineage.latest("daily_bar")

    found: list[Issue] = []
    security = _try_load_security()
    if security is not None:
        recent = _recent_window(df)
        found.extend(validators.check_limit_breach(recent, security))
    calendar = _try_load_calendar(df)
    if calendar:
        found.extend(validators.check_calendar_alignment(
            sorted(set(df["trade_date"].to_list())), calendar))
    found.extend(validators.check_coverage(df))
    found.extend(validators.check_zombie(df))
    found.extend(validators.check_adj_factor(df))

    try:
        save_issues(found, data_version=data_version)
    except Exception as e:  # noqa: BLE001  同上：检查结果落库失败不抛
        from loguru import logger
        logger.error(f"issue 落库失败: {e}")
    return found


def _recent_window(df: pl.DataFrame) -> pl.DataFrame:
    """近端窗口：涨跌停检查的 board/is_st 是当前快照，历史 ST 状态
    不可知 —— 只对最近约 400 个自然日内的数据判定，远端误报毫无价值。"""
    max_d = df["trade_date"].max()
    if max_d is None:
        return df
    cutoff = max_d - timedelta(days=_LIMIT_WINDOW_DAYS)
    return df.filter(pl.col("trade_date") >= cutoff)


def _try_load_security() -> pl.DataFrame | None:
    try:
        from lquant.core.db import reader
        with reader() as con:
            return con.execute("SELECT symbol, board, is_st FROM security").pl()
    except Exception:
        return None


def _try_load_calendar(df: pl.DataFrame) -> list:
    try:
        from lquant.data.store.catalog import TradeCalendarRepo
        return TradeCalendarRepo().range(df["trade_date"].min(), df["trade_date"].max())
    except Exception:
        return []
