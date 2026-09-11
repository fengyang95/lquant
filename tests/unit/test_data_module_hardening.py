"""数据模块硬化回归：provider 契约、parquet 湖语义、主源可配置。

覆盖三类曾经真实存在的坑：
1. `securities(day)` 在 base / fallback / 各 provider 之间签名不一致 ——
   `lq data reference` 走 FallbackProvider 必然 TypeError；
2. 「幽灵表」：DDL 里有 DuckDB `daily_bar` 表，但日线只入 parquet 湖，
   查表恒空且失败是静默的；
3. 主源（`providers_order`）只对跨源对拍生效，回填/增量仍按 yaml 取链头，
   前端「保存并立即生效」是假的。
"""

from __future__ import annotations

import inspect
from datetime import date

import polars as pl

from lquant.data.base import DataProvider, source_name
from lquant.data.capability import Capability
from lquant.data.fallback import FallbackProvider


# ---------------------------------------------------------------- 契约：securities


class _RefProvider(DataProvider):
    """最小 reference provider：记录收到的 day。"""

    name = "stub"
    capability = frozenset({Capability.REFERENCE})

    def __init__(self) -> None:
        self.seen_day: object = "<unset>"

    def daily_bars(self, *a, **k): ...

    def minute_bars(self, *a, **k): ...

    def adj_factors(self, *a, **k): ...

    def financial_pit(self, *a, **k): ...

    def securities(self, day: object = None):
        self.seen_day = day
        return pl.DataFrame()

    def trade_calendar(self, *a, **k): ...


def test_fallback_securities_forwards_day() -> None:
    """reference.sync_securities 传的 day 必须原样到达底层源。"""
    p = _RefProvider()
    fb = FallbackProvider([p])
    d = date(2026, 1, 5)

    fb.securities(d)

    assert p.seen_day == d


def test_fallback_records_serving_source() -> None:
    """血缘要标实际服务源，不能标链头。"""
    p = _RefProvider()
    fb = FallbackProvider([p])

    fb.securities(None)

    assert fb.last_source == "stub"
    assert source_name(fb) == "stub"


def test_every_registered_provider_securities_accepts_day() -> None:
    """所有已注册 provider 的 securities 都必须接受 day（统一契约）。"""
    from lquant.data.providers import PROVIDERS, _import_all

    _import_all()
    for name in PROVIDERS.keys():
        cls = PROVIDERS.get(name)
        method = getattr(cls, "securities", None)
        assert method is not None, f"{name} 缺 securities"
        params = inspect.signature(method).parameters
        assert "day" in params, f"{name}.securities 不接受 day，路由层会 TypeError"


def test_sync_securities_does_not_typeerror_with_stub(monkeypatch) -> None:
    """回归：sync_securities 走 get_provider().securities(day) 不得 TypeError。"""
    from lquant.data.ingest import reference

    fb = FallbackProvider([_RefProvider()])
    # sync_securities 内是「函数内 import」，必须打在 providers 模块属性上
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: fb)

    assert reference.sync_securities() == 0  # 空结果 → 0，但链路已走通


# ---------------------------------------------------------------- parquet 湖语义


def _lake(tmp_path, monkeypatch):
    import lquant.data.store.parquet as pq

    monkeypatch.setattr(pq, "_root", lambda: tmp_path / "lake")
    return pq


def _bars(symbols: list[str], d: date, *, close: float = 10.0, amount: float = 1e7):
    n = len(symbols)
    return pl.DataFrame({
        "symbol": symbols,
        "trade_date": [d] * n,
        "open": [close] * n, "high": [close] * n,
        "low": [close] * n, "close": [close] * n,
        "volume": [1e6] * n, "amount": [amount] * n,
    })


def test_empty_lake_reads_back_schema_shaped(tmp_path, monkeypatch) -> None:
    """空湖必须读出「有 schema 的空帧」，而不是 0 列帧（下游 select 会炸）。"""
    pq = _lake(tmp_path, monkeypatch)

    out = pq.read_daily().select(["symbol", "trade_date"]).collect()

    assert out.height == 0
    assert out.columns == ["symbol", "trade_date"]


def test_daily_range_and_latest_top_by_amount(tmp_path, monkeypatch) -> None:
    pq = _lake(tmp_path, monkeypatch)

    assert pq.daily_range() == (None, None)
    assert pq.latest_trade_date() is None
    assert pq.latest_top_by_amount() == []

    pq.write_daily(_bars(["000001.SZ", "600000.SH"], date(2024, 3, 1), amount=1e7))
    pq.write_daily(_bars(["300750.SZ"], date(2024, 3, 2), amount=5e8))

    assert pq.daily_range() == (date(2024, 3, 1), date(2024, 3, 2))
    # 只看最近交易日（03-02），不跨日累计
    assert pq.latest_top_by_amount(5) == ["300750.SZ"]


def test_write_daily_overwrite_is_idempotent(tmp_path, monkeypatch) -> None:
    """同 (symbol, trade_date) 重投 = 覆盖，不产生重复行。"""
    pq = _lake(tmp_path, monkeypatch)

    pq.write_daily(_bars(["000001.SZ", "600000.SH"], date(2024, 3, 1)))
    pq.write_daily(_bars(["000001.SZ"], date(2024, 3, 1), close=11.0))

    df = pq.read_daily().collect()
    assert df.height == 2
    got = df.filter(pl.col("symbol") == "000001.SZ")["close"].to_list()
    assert got == [11.0]


def test_write_daily_dedup_survives_dtype_drift(tmp_path, monkeypatch) -> None:
    """湖内主键列类型与新区块不同时，去重仍必须生效（旧 struct.is_in 会静默失效）。"""
    pq = _lake(tmp_path, monkeypatch)

    pq.write_daily(_bars(["000001.SZ"], date(2024, 3, 1)))
    # 用 Datetime 形态的 trade_date 再投一次（模拟跨源/跨版本类型漂移）
    drifted = _bars(["000001.SZ"], date(2024, 3, 1), close=12.0).with_columns(
        pl.col("trade_date").cast(pl.Datetime)
    )
    pq.write_daily(drifted)

    df = pq.read_daily().collect()
    assert df.height == 1, f"主键漂移下去重失效，出现重复行: {df.height}"


def test_lake_glob_is_absolute(tmp_path, monkeypatch) -> None:
    """SQL 侧 glob 必须绝对 —— 相对路径会随进程 CWD 漂移读到空集。"""
    pq = _lake(tmp_path, monkeypatch)

    assert pq.lake_glob("daily").startswith(str(tmp_path))
    assert pq.lake_glob("daily").endswith("*.parquet")


def test_ensure_views_skips_empty_lake(tmp_path) -> None:
    """empty lake must not break `init_db`：DuckDB 建视图时就解析 read_parquet schema。"""
    import duckdb

    from lquant.data.store.ddl import ensure_views

    con = duckdb.connect(":memory:")
    assert ensure_views(con, tmp_path) == 0

    p = tmp_path / "daily" / "year=2026" / "part-0.parquet"
    p.parent.mkdir(parents=True)
    _bars(["000001.SZ"], date(2026, 9, 10)).write_parquet(p)

    assert ensure_views(con, tmp_path) == 1
    assert con.execute("SELECT count(*) FROM v_daily").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------- 主源可配置


def test_build_chain_honors_providers_order(monkeypatch) -> None:
    """providers_order 首位 = 主源，必须反映到 Fallback 链顺序。"""
    from lquant.data import providers as pv

    order = [n for n in ("tencent", "baostock") if n in pv.PROVIDERS]
    monkeypatch.setattr(
        pv, "_reorder_by_settings",
        lambda chain: sorted(
            chain,
            key=lambda p: order.index(p.name) if p.name in order else len(order),
        ),
    )
    pv.reset_chain()
    try:
        chain = pv.build_chain()
        names = [p.name for p in chain.providers]
        assert names[: len(order)] == order
    finally:
        pv.reset_chain()


def test_settings_put_invalidates_chain_cache() -> None:
    """写 providers_order 后 build_chain 缓存必须失效（否则要重启才生效）。"""
    from lquant.data import providers as pv
    from lquant.core.settings_store import SettingsStore

    calls = {"n": 0}
    real = pv.reset_chain

    def _spy() -> None:
        calls["n"] += 1
        real()

    pv.reset_chain = _spy  # type: ignore[assignment]
    try:
        SettingsStore._invalidate_provider_cache("providers_order")
        SettingsStore._invalidate_provider_cache("crosscheck_peers")
        SettingsStore._invalidate_provider_cache("timezone")
    finally:
        pv.reset_chain = real  # type: ignore[assignment]

    assert calls["n"] == 2, "只有影响 provider 链的 key 该触发失效"
