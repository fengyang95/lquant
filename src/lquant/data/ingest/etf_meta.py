"""ETF 元数据：跟踪指数、T+N、份额、IOPV。

为什么单独一张表：
- ETF 的 T+N 是 **per-instrument** 的：跨境/债券/黄金/货币 ETF 是 T+0，
  普通股票 ETF 是 T+1。用全局规则会直接把回测成交做错。
- 免印花税也是 ETF 特有（规则表里 sec_type=etf 分支处理）。

数据来源分层：
1. BaoStock 给得到 symbol/name + 名称推断的 track_index 与 sellable_after_days
2. 费率/份额/规模 BaoStock 没有，靠 enrich_from_akshare() 用东财快照补
   （可选，akshare 未安装或网络不通就跳过，不影响主链路）
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.types import now_cn
from lquant.data.store.catalog import EtfMetaRepo, SecurityRepo


def sync_etf_meta(symbols: list[str] | None = None) -> int:
    """从主源同步 ETF 元数据。幂等。"""
    from loguru import logger

    from lquant.data.providers import get_provider

    provider = get_provider()
    target = provider.providers[0] if hasattr(provider, "providers") else provider
    df = target.etf_meta(symbols)
    if not len(df):
        logger.warning("ETF 元数据返回为空")
        return 0
    n = EtfMetaRepo().upsert(df)
    t0 = int((df["sellable_after_days"] == 0).sum()) if "sellable_after_days" in df else 0
    logger.info(f"ETF 元数据 {n} 只（推断 T+0: {t0} 只）")
    return n


def enrich_from_akshare() -> int:
    """用东财 ETF 实时快照补规模/份额。失败不影响主链路。"""
    from loguru import logger

    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        logger.info("akshare 未安装，跳过 ETF 元数据增强（pip install akshare）")
        return 0

    repo = EtfMetaRepo()
    universe = set(SecurityRepo().etf_symbols())
    try:
        pdf = ak.fund_etf_spot_em()
    except Exception as e:  # noqa: BLE001 - 网络/接口变更都吞掉
        logger.warning(f"东财 ETF 快照拉取失败，跳过: {e}")
        return 0

    col = "代码" if "代码" in pdf.columns else None
    if col is None:
        return 0
    from lquant.core.types import parse_symbol

    pdf = pdf.copy()
    pdf["symbol"] = [str(parse_symbol(str(c))) for c in pdf[col]]
    pdf = pdf[pdf["symbol"].isin(universe)]
    if pdf.empty:
        return 0

    mapping = {"流通市值": "fund_size", "成交额": "amount", "名称": "name"}
    out = pd.DataFrame({"symbol": pdf["symbol"]})
    for src, dst in mapping.items():
        if src in pdf.columns:
            out[dst] = pdf[src].values
    if "fund_size" in out.columns:
        out["fund_size"] = out["fund_size"] / 1e8      # 元 → 亿元
    out["as_of"] = date.today()
    out["source"] = "akshare"

    df = pl.from_pandas(out)
    n = repo.upsert(df)
    logger.info(f"ETF 元数据增强 {n} 只（规模/份额）")
    return n


def sync_etf(enrich: bool = False) -> dict:
    out = {"etf_meta": sync_etf_meta()}
    if enrich:
        out["enrich"] = enrich_from_akshare()
    return out


__all__ = ["sync_etf_meta", "enrich_from_akshare", "sync_etf"]
