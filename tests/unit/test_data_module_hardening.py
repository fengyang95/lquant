"""数据模块硬化回归：血缘源标注、parquet 湖语义、主源可配置。

覆盖三类曾经真实存在的坑：
1. 血缘 `source` 硬编码 `baostock` —— Fallback 切到辅源后血缘失真；
2. 「幽灵表」：DDL 里有 DuckDB `daily_bar` 表，但日线只入 parquet 湖，
   查表恒空且失败是静默的；空湖还读出 0 列帧，下游 `.select()` 直接炸，
   且「没同步过」与「窗口内无该标的」两种空无法区分；
3. 主源（`providers_order`）只对跨源对拍生效，回填/增量仍按 yaml 取链头，
   前端「保存并立即生效」是假的。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.data.base import DataProvider, source_name
from lquant.data.capability import Capability
from lquant.data.fallback import FallbackProvider

# ---------------------------------------------------------------- 契约：securities


class _RefProvider(DataProvider):
    """最小 reference provider：securities 返回空清单（走通链路用）。"""

    name = "stub"
    capability = frozenset({Capability.REFERENCE})

    def daily_bars(self, *a, **k): ...

    def minute_bars(self, *a, **k): ...

    def adj_factors(self, *a, **k): ...

    def financial_pit(self, *a, **k): ...

    def securities(self):
        return pl.DataFrame()

    def trade_calendar(self, *a, **k): ...


def test_fallback_records_serving_source() -> None:
    """血缘要标实际服务源，不能标链头。"""
    p = _RefProvider()
    fb = FallbackProvider([p])

    fb.securities()

    assert fb.last_source == "stub"
    assert source_name(fb) == "stub"


def test_source_name_falls_back_to_name_without_last_source() -> None:
    """还没服务过任何请求时（last_source 未置）应回落到 name。"""
    assert source_name(_RefProvider()) == "stub"


def test_sync_securities_stamps_actual_serving_source(monkeypatch) -> None:
    """sync_securities 的血缘 source 必须是实际服务源。

    原先硬编码 `pl.lit("baostock")`：Fallback 切到辅源后会把辅源的数据
    标成主源，血缘失真且不可追。
    """
    from lquant.data.ingest import reference

    seen: dict[str, list[str]] = {}

    class _Repo:
        def upsert(self, df) -> int:
            seen["source"] = df["source"].to_list()
            return len(df)

    class _Prov(_RefProvider):
        name = "tushare"

        def securities(self):
            return pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]})

    fb = FallbackProvider([_Prov()])
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: fb)
    monkeypatch.setattr(reference, "SecurityRepo", _Repo)
    monkeypatch.setattr(reference, "_merge_existing_details", lambda df, **k: df)

    assert reference.sync_securities() == 1
    assert seen["source"] == ["tushare"]


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


# ---------------------------- 「湖为空」vs「湖有数据但没命中」的区分


def test_lake_is_empty_distinguishes_missing_from_empty_result(
        tmp_path, monkeypatch) -> None:
    """两种「空」语义不同，必须能区分。

    读函数对空湖返回「有 schema 的空帧」，所以「帧是空的」既可能是
    没同步过（该提示先同步）、也可能是窗口内确实没这只标的（正常空结果）。
    早期实现靠「返回 0 列帧」来推断前者，代价是任何 `.select()` 都会炸。
    """
    pq = _lake(tmp_path, monkeypatch)

    assert pq.lake_is_empty("daily") is True

    pq.write_daily(_bars(["000001.SZ"], date(2024, 3, 1)))

    assert pq.lake_is_empty("daily") is False
    # 湖里有数据，但过滤到一个不存在的标的 → 帧为空、湖不为空
    assert pq.read_daily(symbols=["999999.SZ"]).collect().is_empty()
    assert pq.lake_is_empty("daily") is False


def test_lake_is_empty_is_per_partition(tmp_path, monkeypatch) -> None:
    """日线有数据不代表分钟线分区非空（各自独立探测）。"""
    pq = _lake(tmp_path, monkeypatch)

    pq.write_daily(_bars(["000001.SZ"], date(2024, 3, 1)))

    assert pq.lake_is_empty("daily") is False
    assert pq.lake_is_empty("minute/freq=60min") is True


def test_history_empty_lake_says_sync_first(tmp_path, monkeypatch) -> None:
    """全新 checkout：报可读的「数据根目录为空」，不是 polars 裸异常。"""
    _lake(tmp_path, monkeypatch)
    from lquant.research.dialect import jq_shim

    jq_shim.bind(jq_shim.JQContext(
        engine=None, trade_date=date(2026, 6, 12), universe=["600519.SH"]))
    try:
        with pytest.raises(ValueError, match="数据根目录为空"):
            jq_shim.history(3, "1d", "close")
    finally:
        jq_shim.bind(None)


def test_history_nonempty_lake_reports_window_not_missing_lake(
        tmp_path, monkeypatch) -> None:
    """湖里有数据、只是该标的没 bar → 报窗口型错误，不能误报「没同步过」。"""
    pq = _lake(tmp_path, monkeypatch)
    pq.write_daily(_bars(["000001.SZ"], date(2026, 6, 10)))
    from lquant.research.dialect import jq_shim

    jq_shim.bind(jq_shim.JQContext(
        engine=None, trade_date=date(2026, 6, 12), universe=["600519.SH"]))
    try:
        with pytest.raises(ValueError, match="窗口 3 天"):
            jq_shim.history(3, "1d", "close")
    finally:
        jq_shim.bind(None)


def test_valuation_frame_empty_lake_returns_empty_tables(
        tmp_path, monkeypatch) -> None:
    """get_fundamentals 的估值列在空湖下返回空表（不抛、不误报）。"""
    _lake(tmp_path, monkeypatch)
    from lquant.research.dialect.fundamentals import _valuation_frame, valuation

    out = _valuation_frame([valuation.pe_ratio], ["600519.SH"], date(2026, 6, 12))

    assert list(out.values()) == [{}]


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
    from lquant.core.settings_store import SettingsStore
    from lquant.data import providers as pv

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
