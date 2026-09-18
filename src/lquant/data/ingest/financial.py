"""PIT 财务入库：stat_date（报告期）+ pub_date（公告日）双日期。

防未来函数的最后一道闸：任何财务因子在 T 日只能用 pub_date <= T 的数据。
BaoStock 按 (code, year, quarter) 单点查询无批量接口，所以：
- 标的池必须受限（默认中证 800 / 自选池）
- 每只标的跑完就写 checkpoint，中断可续

**断点是「窗口 + 标的」二维的**（2026-09-18 修）：tushare/baostock 的
start/end 过滤的是**公告日**，所以「这只补过了」只在某个公告日区间内成立。
早先只按 symbol 记 done，全市场回填（2016 起）把 5898 只标成 done 之后，
每日 90 天窗口的增量作业 remaining 恒为空 —— 新公告、新上市标的永远进不来，
而作业状态还是 ok（静默缺口）。现在走 Checkpoint 的覆盖区间记账，并按
每只标的已覆盖到哪天只拉增量段（常见情形只需一次请求）。
"""
from __future__ import annotations

from datetime import date, timedelta

from lquant.core.types import today_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.catalog import FinancialRepo


def _todo_windows(cp: Checkpoint, symbols: list[str],
                  start_d: date, end_d: date) -> dict[date, list[str]]:
    """按「需要补拉的起点」把标的分组：{eff_start: [symbol, ...]}。

    - 已有区间完整覆盖 [start_d, end_d] → 跳过
    - 已覆盖到 hi 但请求窗口更晚 → 只拉 [hi+1, end_d]（公告日增量）
    - 已覆盖区间起点晚于请求起点（请求更早）→ 从 start_d 整段重拉
      （宁可重复拉，也不漏更早公告日的报告）
    - 没记过区间（老 checkpoint / 新标的）→ 从 start_d 整段拉

    返回空 dict 表示「该窗口确实无事可做」—— 与「被断点静默跳过」在日志
    里是两个不同结论，调用方据此区分。
    """
    groups: dict[date, list[str]] = {}
    for sym in dict.fromkeys(symbols):  # 去重：重复标的会让 pending/skipped 口径错乱
        span = cp.covered_window(sym)
        if span is None:
            eff = start_d
        else:
            lo, hi = span
            if lo <= start_d and hi >= end_d:
                continue
            eff = start_d if lo > start_d else hi + timedelta(days=1)
        if eff > end_d:
            continue
        groups.setdefault(eff, []).append(sym)
    return groups


def backfill_financial(
    symbols: list[str],
    start: date | str = "2016-01-01",
    end: date | str | None = None,
    kinds: tuple[str, ...] = (
        "profit", "balance", "cashflow", "dupont", "growth", "operation",
    ),
    provider_name: str | None = None,
    batch: int = 20,
) -> dict:
    """PIT 财务回填。

    基本面统一 tushare（2026-09-13 起约定）：provider_name=None 时优先
    取链中 tushare（income/balancesheet/cashflow + fina_indicator 全指标，
    天然 ann_date；dupont/growth/operation 口径并入 indicator），缺 token
    或未注册时退回链头（baostock，六张季表）。
    checkpoint 键含源名：换源重跑不会误跳过。

    Returns:
        {"done": 本次实际拉取的标的数, "skipped_covered": 被覆盖区间跳过的
         标的数, "groups": 增量段个数, "start"/"end": 请求窗口, "rows": 表内
         总行数}。「全被跳过」与「拉了一遍但源零返回」必须能区分 ——
        前者是断点命中，后者是数据缺口。
    """
    from loguru import logger

    from lquant.data.providers import get_provider

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else (date.fromisoformat(end) if end else today_cn())

    chain = get_provider()
    if provider_name:
        matches = [p for p in chain.providers if p.name == provider_name]
        if not matches:
            raise RuntimeError(f"provider {provider_name} 不可用（未启用或缺 token）")
        target = matches[0]
    elif not hasattr(chain, "providers"):
        target = chain
    else:
        ts = [p for p in chain.providers if p.name == "tushare"]
        target = ts[0] if ts else chain.providers[0]
    cp_name = f"financial_pit_{target.name}"

    cp = Checkpoint(cp_name)
    groups = _todo_windows(cp, list(symbols), start_d, end_d)
    pending = sum(len(v) for v in groups.values())
    skipped = len(set(symbols)) - pending
    logger.info(
        f"财务回填[{target.name}] 待拉 {pending} 只 / 已覆盖跳过 {skipped} 只 "
        f"/ 增量段 {len(groups)} 个，窗口 {start_d}~{end_d}")

    repo = FinancialRepo()
    done = 0
    for eff_start in sorted(groups):
        syms = groups[eff_start]
        logger.info(f"  增量段 {eff_start}~{end_d}：{len(syms)} 只")
        for i in range(0, len(syms), batch):
            chunk = syms[i : i + batch]
            try:
                df = target.financial_pit(chunk, eff_start, end_d, kinds)
            except Exception as e:  # noqa: BLE001 - 单批失败不应炸掉整个任务
                # 失败批不标记完成：否则 transient 网络错误会把这批标的永久
                # 记为 done，重跑全部跳过 —— 基本面静默缺失。覆盖区间一并
                # 清掉，避免「区间说覆盖了、其实这批没拉」。
                logger.warning(f"批次 {eff_start}/{i} 失败，未标记（重跑将重试）: {e}")
                cp.unmark(chunk)
                continue
            if len(df):
                repo.upsert(df)
            # 覆盖区间在源调用成功后才记（0 行也是成功 —— 该公告日窗口里
            # 确实没有新报告），与退市/无财报标的的语义一致
            cp.record_coverage(chunk, eff_start, end_d)
            done += len(chunk)
            if done % 100 == 0:
                logger.info(f"  财务进度 {done}/{pending}")
    logger.info(f"财务回填完成 {done} 只（覆盖跳过 {skipped} 只），"
                f"累计 {repo.count()} 条记录")
    return {"done": done, "skipped_covered": skipped, "groups": len(groups),
            "start": start_d.isoformat(), "end": end_d.isoformat(),
            "rows": repo.count()}


__all__ = ["backfill_financial"]
