"""PIT 财务入库：stat_date（报告期）+ pub_date（公告日）双日期。

防未来函数的最后一道闸：任何财务因子在 T 日只能用 pub_date <= T 的数据。
BaoStock 按 (code, year, quarter) 单点查询无批量接口，所以：
- 标的池必须受限（默认中证 800 / 自选池）
- 每只标的跑完就写 checkpoint，中断可续
"""
from __future__ import annotations

from datetime import date

from lquant.core.types import today_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.catalog import FinancialRepo


def backfill_financial(
    symbols: list[str],
    start: date | str = "2016-01-01",
    end: date | str | None = None,
    kinds: tuple[str, ...] = (
        "profit", "balance", "cashflow", "dupont", "growth", "operation",
    ),
    provider_name: str | None = None,
    batch: int = 20,
) -> int:
    """PIT 财务回填。

    provider_name=None 走 Fallback 链头（默认 baostock，六张季表全支持）；
    指定 "tushare" 时用 tushare pro（income/balancesheet/cashflow/
    fina_indicator 四类，天然 ann_date），适合与 baostock 互为补充/对拍。
    checkpoint 键含源名：换源重跑不会误跳过。
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
        cp_name = f"financial_pit_{provider_name}"
    else:
        target = chain.providers[0] if hasattr(chain, "providers") else chain
        cp_name = "financial_pit"

    cp = Checkpoint(cp_name)
    todo = cp.remaining(list(symbols))
    logger.info(f"财务回填[{target.name}] {len(todo)} 只（已完成 {len(cp)}）{start_d}~{end_d}")

    repo = FinancialRepo()
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        try:
            df = target.financial_pit(chunk, start_d, end_d, kinds)
        except Exception as e:  # noqa: BLE001 - 单批失败不应炸掉整个任务
            logger.warning(f"批次 {i} 失败，跳过: {e}")
            cp.mark(chunk)
            continue
        if len(df):
            repo.upsert(df)
        cp.mark(chunk)
        done += len(chunk)
        if done % 100 == 0:
            logger.info(f"  财务进度 {done}/{len(todo)}")
    logger.info(f"财务回填完成 {done} 只，累计 {repo.count()} 条记录")
    return done


__all__ = ["backfill_financial"]
