"""质量门禁：同步链路的统一入口。

写入湖之前跑记录级断言（fatal 阻断），落库后可对全湖跑
序列/截面级 validators（`lq data check`）。issue 一律落
data_quality_issue 表 —— 标记而非删除。
"""
from __future__ import annotations

from datetime import timedelta

import polars as pl

from lquant.core.errors import DataQualityError
from lquant.core.logging import get_logger
from lquant.data.quality import adjustment, asserts, universe, validators
from lquant.data.quality.issues import Issue, save_issues

log = get_logger(__name__)

__all__ = ["gate_daily", "run_lake_checks", "check_lake_structure"]


def check_lake_structure() -> list[Issue]:
    """只跑结构性检查（数据根 + 分区连续性），不读全湖日线帧。

    给「启动自检 / 同步后门禁」这类**要求廉价且必须跑**的场景用：
    run_lake_checks 会物化整个日线帧，不适合每次同步后都调。
    """
    from lquant.data.store import integrity

    found: list[Issue] = []
    for label, probe in (
        ("数据根自检", integrity.check_data_root),
        ("分区连续性检查",
         lambda: integrity.check_partition_continuity(_try_load_full_calendar())),
    ):
        try:
            found.extend(probe())
        except Exception as e:  # noqa: BLE001
            log.warning(f"{label}跳过: {e}")
    return found

# 涨跌停检查只用当前 board/is_st 快照（PIT 局限），只跑近端窗口
_LIMIT_WINDOW_DAYS = 400


def gate_daily(df: pl.DataFrame, *, data_version: str | None = None,
               raise_on_fatal: bool = True) -> tuple[pl.DataFrame, list[Issue]]:
    """日线批次入湖前的门禁：主键去重 + 八项断言（记录级）+ 打 quality_flags。

    fatal 失败抛 DataQualityError 阻断下游（§3.8.6）；
    warn 只打标 + 落 issue，批次照常落地。
    """
    out, found = asserts.run_record_checks(df, raise_on_fatal=False)
    # 主键去重（fatal）：重复 (symbol, trade_date) 行静默入湖后，upsert 语义
    # 下会互相覆盖且读取侧无法察觉 —— 必须在写湖前拦住
    keys = [k for k in ("symbol", "trade_date") if k in out.columns]
    if keys:
        n_dup = len(out) - len(out.unique(subset=keys))
        if n_dup:
            found.append(Issue(
                rule="DUP_KEY", severity="fatal",
                dataset=found[0].dataset if found else "daily_bar",
                detail=f"{keys} 有 {n_dup} 行重复键 —— upsert 会互相覆盖，读取侧不可见",
                count=n_dup))
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

    if data_version is None:
        from lquant.data import lineage
        data_version = lineage.latest("daily_bar")

    found: list[Issue] = []

    # 结构性检查**先跑，且不受「日线帧为空」影响**：湖整年缺失时帧恰恰可能
    # 为空或依然「看起来正常」（latest_trade_date 仍是最新），早退会让最该
    # 报的问题永远不报。实测真实湖整年缺 2025 却无人发现（审计 A1/A2）。
    found.extend(check_lake_structure())

    df = read_daily(start=start, end=end).collect()
    if not len(df):
        _save_lake_issues(found, data_version)
        return found

    # 只挑检查需要的列 —— 全湖宽表全量物化是纯浪费（M5）
    keep = [c for c in ("symbol", "trade_date", "open", "high", "low",
                        "close", "pre_close", "volume", "adj_factor")
            if c in df.columns]
    df = df.select(keep)

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

    # 复权一致性对账（孤儿模块接线）：issues 只并入 found，由尾部
    # save_issues 统一落库；打标副本（out df）不写回 —— lake 检查只读
    try:
        _, adj_issues = adjustment.check_adjustment(df)
        found.extend(adj_issues)
    except Exception as e:  # noqa: BLE001  检查自身不能成为链路单点故障
        from loguru import logger
        logger.warning(f"adjustment 检查跳过: {e}")

    # 时点股票池检查（幸存者偏差）：只读统计，风险仅打标不阻断
    try:
        pit = universe.check_point_in_time(df)
        if pit.get("survivorship_risk"):
            reasons = pit.get("reasons") or []
            found.append(Issue(
                rule="SURVIVORSHIP_RISK", severity="warn",
                dataset="daily_bar",
                detail=("; ".join(reasons) if reasons
                        else "survivorship risk")[:500],
                count=1))
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"universe 检查跳过: {e}")

    # golden 已知答案集：表缺失/无 case 降级跳过，不能拖垮检查链路
    try:
        from lquant.data.quality import golden
        found.extend(r.as_issue() for r in golden.run_all() if not r.ok)
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"golden 检查跳过: {e}")

    # 不接 tradability：flag_tradability 是写回型打标（改湖内数据），
    # lake 检查只读不写回，接线无意义。

    _save_lake_issues(found, data_version)
    return found


def _save_lake_issues(found: list[Issue], data_version: str | None) -> None:
    """issue 落库失败不抛 —— 检查结果落不了库也不该阻断同步链路。"""
    try:
        save_issues(found, data_version=data_version)
    except Exception as e:  # noqa: BLE001
        log.error(f"issue 落库失败: {e}")


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
            # sec_type / list_date 是涨跌停检查的必需列：板性要从代码段推
            # （board 对股票全为 NULL），上市天数决定新股豁免（见 validators）
            return con.execute(
                "SELECT symbol, board, is_st, sec_type, list_date FROM security").pl()
    except Exception:
        log.exception("security 快照加载失败，board/is_st 检查降级跳过")
        return None


def _try_load_calendar(df: pl.DataFrame) -> list:
    try:
        from lquant.data.store.catalog import TradeCalendarRepo
        return TradeCalendarRepo().range(df["trade_date"].min(), df["trade_date"].max())
    except Exception:
        log.exception("交易日历加载失败，涨跌停检查降级跳过")
        return []


def _try_load_full_calendar() -> list:
    """全量交易日历（供分区连续性检查用）。

    不能用日线帧的 min/max：帧本身缺整年时 min/max 依然是一个「正常」的
    跨度（2021~2026 之间缺 2025），连续性检查必须拿到完整年份才知道哪一年
    本该有数据。日历表很小（约 1.3 万行），全量查询代价可忽略。
    """
    try:
        from datetime import date as _date

        from lquant.core.types import today_cn
        from lquant.data.store.catalog import TradeCalendarRepo
        return TradeCalendarRepo().range(_date(1990, 1, 1),
                                        _date(today_cn().year + 1, 12, 31))
    except Exception:
        log.exception("交易日历加载失败，分区连续性检查降级跳过")
        return []
