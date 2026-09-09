"""跨源对拍编排（§3.8.4 抽检）。

湖内为主源，同行 provider 实拉对拍。核心原则：比对只用于标记与降级，
绝不把 peer 的数值写回湖 —— 湖永远只信 primary。

流程：
  1. 从湖读主源日线（哨兵抽样）
  2. 用 PROVIDERS 直接实例化 peer（不经过生产 yaml 的 enabled 门，
     对拍是运维动作，不该被同步同源开关挡住）
  3. classify_divergence → 分档
  4. 对 L2/L3 打 primary 的 CROSS_SRC_DIFF 标记（flag_cross_source），
     并把标记**写回湖**（§3.8.4 读取侧据此降级）
  5. 落 data_quality_issue（可检索、可 resolve）
  6. 返回 summary

peer 不可用/未安装/未声明 daily → 整体 L0 返回，不视为失败（对拍不该
成为同步链路单点故障 —— 与 pipeline 里其余检查同原则）。

抽检窗口：primary 与 peer 使用同一 [start, end]，否则 peer 多拉出窗口外
的数据会全部落进"同键缺失"桶，污染分级。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality import crosscheck
from lquant.data.quality.issues import Issue, save_issues

__all__ = ["run_crosscheck", "validate_peers"]


def validate_peers(names: list[str]) -> None:
    """对拍 peer 源合法性校验：已注册 + 声明 daily/etf_daily capability。

    给 SettingsStore.put（API 层 ValueError → 422）与运维入口共用。
    capability 取 provider 类的声明（类属性），不实例化、不触网。
    """
    from lquant.data.capability import Capability
    from lquant.data.providers import PROVIDERS, _import_all

    _import_all()
    for name in names:
        if name not in PROVIDERS:
            raise ValueError(
                f"未知数据源 {name!r}，可选: {PROVIDERS.keys()}")
        caps = PROVIDERS.get(name).capability
        if not (Capability.DAILY in caps or Capability.ETF_DAILY in caps):
            raise ValueError(
                f"对拍 peer {name!r} 未声明 daily/etf_daily capability，不可用")


def _cfg() -> dict:
    """对拍配置：SettingsStore 覆盖 > providers.yaml crosscheck 节。

    - peers：settings 的 crosscheck_peers（非空）> yaml crosscheck.peers
    - 主源：settings providers_order 首位 > yaml crosscheck.primary
      （不另设 key，与执行器共用一个语义，改完配置下一次拉取即生效）
    - enabled / tolerance_pct / fields 仍读 yaml
    """
    from lquant.core.config import load_providers
    from lquant.core.settings_store import SettingsStore

    ycfg = load_providers().get("crosscheck", {})
    try:
        runtime = {i["key"]: i["value"] for i in SettingsStore().all()}
    except Exception:  # noqa: BLE001 - 表不可用时退回纯 yaml 配置
        runtime = {}
    order = list(runtime.get("providers_order") or [])
    peers = list(runtime.get("crosscheck_peers") or [])
    return {
        "enabled": bool(ycfg.get("enabled", True)),
        "primary": (order[0] if order else None) or ycfg.get("primary"),
        "peers": peers or list(ycfg.get("peers", [])),
        "tolerance_pct": ycfg.get("tolerance_pct", 0.1),
        "fields": tuple(ycfg.get(
            "fields", ["open", "high", "low", "close", "pre_close", "volume"])),
    }


def _peer_daily(name: str, symbols: list[str], start: date, end: date) -> pl.DataFrame:
    """实例化 peer，拉日线；不支持/缺失 → 返回空 df。"""
    try:
        from lquant.data.capability import Capability
        from lquant.data.providers import PROVIDERS

        cls = PROVIDERS.get(name)
        if cls is None:
            return pl.DataFrame()
        prov = cls()
        if not prov.has(Capability.DAILY):
            return pl.DataFrame()
        df = prov.daily_bars(symbols, start, end)
        return df
    except Exception as e:  # noqa: BLE001  对拍拉数失败 → 当无此源处理
        from loguru import logger
        logger.warning(f"crosscheck peer {name} 拉数失败: {e}")
        return pl.DataFrame()


def _sample_symbols(limit: int) -> tuple[list[str], date, date]:
    """哨兵抽样的标的 + 日期范围。

    §3.8.4 本偏好指数哨兵作"价格基准"，但当前可用的 daily peer（akshare）
    没有指数日线路由，指数会打到股票接口 000001.SH→平安银行 造成假 L3。
    故对拍抽样用股票哨兵，直到存在 index_daily 的 peer 再切回。
    """
    from lquant.core.db import reader

    with reader() as con:
        # 一次连接内完成：优先非 ST 股票，避免指数无 index_daily 路由的假偏差。
        rows = con.execute(
            "SELECT symbol FROM security WHERE sec_type='stock' AND is_st=0 "
            "ORDER BY symbol LIMIT ?", [limit]).fetchall()
        if not rows:
            rows = con.execute(
                "SELECT symbol FROM security WHERE sec_type='index' AND "
                "delist_date IS NULL ORDER BY symbol LIMIT ?", [limit]).fetchall()
        d = con.execute(
            "SELECT min(trade_date), max(trade_date) FROM daily_bar").fetchone()
    symbols = [r[0] for r in rows]
    lo, hi = d[0], d[1] if d and d[0] else None
    if lo is None:
        lo, hi = date(2024, 1, 1), date.today()
    return symbols, lo, hi or date.today()


def run_crosscheck(peers: list[str] | None = None, start: str | None = None,
                   end: str | None = None, limit: int = 200) -> dict:
    """抽检主源 vs 同行源，返回 {summary, issues, flagged_rows}。"""
    from lquant.data.store.parquet import write_daily

    cfg = _cfg()
    if not cfg["enabled"]:
        return {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0}

    # CLI 未显式给 peers 时用配置；显式给了则覆盖配置。
    conn_peers = [p for p in (peers or []) if p]
    peers = conn_peers or cfg["peers"]
    if not peers:
        return {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0}

    tolerance_pct = cfg["tolerance_pct"]
    fields = cfg["fields"]

    symbols, lo, hi = _sample_symbols(limit)
    if not symbols:
        return {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0}

    lo_s, hi_s = start or lo.isoformat(), end or hi.isoformat()
    primary = _primary_daily(symbols[:limit], lo_s, hi_s)
    if not len(primary):
        return {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0}

    issues_all: list[Issue] = []
    peer_diffs: list[pl.DataFrame] = []
    # 汇总跨同行源累积：L1/L2/L3 各是"全部 peer 对该窗口所有判级的总和"，
    # 而不是只留最后一个 peer 的计数。
    summary: dict = {"checked": 0, "L1": 0, "L2": 0, "L3": 0}
    for name in peers:
        peer = _peer_daily(name, symbols[:limit], lo, hi)
        if not len(peer):
            from loguru import logger
            logger.warning(f"crosscheck peer {name} 无数据可用，跳过")
            continue
        diffs = crosscheck.classify_divergence(
            primary, peer, tolerance_pct=tolerance_pct, fields=fields)
        if not len(diffs):
            continue
        peer_diffs.append(diffs)
        s = crosscheck.summarize(diffs)
        for k in ("checked", "L1", "L2", "L3"):
            summary[k] += s.get(k, 0)
        # 按 (symbol, date) 汇聚到最高等级 → 落 issue
        levels = diffs.group_by("symbol", "trade_date").agg(
            pl.col("level").max()).filter(pl.col("level") != crosscheck.L1)
        for r in levels.iter_rows(named=True):
            issues_all.append(Issue(
                rule=f"CROSS_SRC_DIFF.{name}.{r['level']}",
                severity="error" if r["level"] == crosscheck.L3 else "warn",
                detail=f"{name} 对拍 {r['level']}：{r['symbol']}@{r['trade_date']}",
                symbol=r["symbol"], trade_date=r["trade_date"],
                count=int((diffs.filter(
                    (pl.col("symbol") == r["symbol"]) &
                    (pl.col("trade_date") == r["trade_date"]))).height)))

    # 跨同行源统一：任一源 L2/L3 即对该 (symbol, date) 打标降级，并持久化。
    flagged_rows = 0
    if peer_diffs:
        flagged_df = crosscheck.flag_cross_source(primary, pl.concat(peer_diffs))
        flagged_rows = int(flagged_df.filter(
            (pl.col("quality_flags") & crosscheck.CROSS_SRC_DIFF) != 0).height)
        if flagged_rows:
            # 只把被标记的行 upsert 回湖 —— 其余不变，幂等、不放大写量。
            flagged = flagged_df.filter(
                (pl.col("quality_flags") & crosscheck.CROSS_SRC_DIFF) != 0)
            try:
                write_daily(flagged)
            except Exception as e:  # noqa: BLE001
                from loguru import logger
                logger.error(f"crosscheck 标记回写湖失败: {e}")

    try:
        save_issues(issues_all)
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.error(f"crosscheck issue 落库失败: {e}")
    # 无 peer 可比 → 整体 L0（与"配置关了对拍"同语义，不视为失败）
    if not peer_diffs:
        summary = {"L0": 1}
    return {"summary": summary, "issues": issues_all, "flagged_rows": flagged_rows}


def _primary_daily(symbols: list[str], start: str, end: str) -> pl.DataFrame:
    """湖内主源日线。取 daily_bar（已按 key upsert，quality_flags 在列里）。"""
    from lquant.data.store.parquet import read_daily

    return read_daily(symbols=symbols, start=start, end=end).collect()