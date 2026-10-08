"""M6b：jq_shim.history 与 get_fundamentals 的防前视语义测试。

用临时 DuckDB（monkeypatch catalog reader/writer）+ 临时 parquet 日线，
离线可跑。核心断言：财务数据严格 pub_date <= t；history 不越过 current_dt。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from lquant.data.store import catalog

pytest.importorskip("pandas")

_DAY = date(2026, 6, 15)


@pytest.fixture()
def tmp_catalog(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "lq.duckdb"
    con = duckdb.connect(str(db))
    from lquant.data.store.ddl import DDL_STATEMENTS

    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    @contextmanager
    def _writer():
        c = duckdb.connect(str(db))
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @contextmanager
    def _reader():
        c = duckdb.connect(str(db))
        try:
            yield c
        finally:
            c.close()

    monkeypatch.setattr(catalog, "writer", _writer)
    monkeypatch.setattr(catalog, "reader", _reader)


@pytest.fixture()
def _bars(tmp_path: Path, monkeypatch):
    """日线写临时 parquet（read_daily 按 LQUANT_DATA_ROOT 读取）。"""
    df = pl.DataFrame({
        "symbol": ["600519.SH"] * 6,
        "trade_date": [date(2026, 6, 10) + __import__("datetime").timedelta(days=i)
                       for i in range(6)],
        "open": [10.0, 10.5, 11.0, 11.5, 12.0, 12.5],
        "high": [10.5, 11.0, 11.5, 12.0, 12.5, 13.0],
        "low": [9.8, 10.2, 10.7, 11.2, 11.7, 12.2],
        "close": [10.2, 10.8, 11.2, 11.8, 12.2, 12.8],
        "volume": [1_000_000.0] * 6,
        "amount": [1e7] * 6,
        "adj_factor": [1.0, 1.0, 1.0, 1.0, 2.0, 2.0],
        "turnover_rate": [1.5, 1.6, 1.7, 1.8, 1.9, 2.0],
        "pe_ttm": [30.0, 31.0, 32.0, 33.0, 34.0, 35.0],
        "pb_mrq": [5.0, 5.1, 5.2, 5.3, 5.4, 5.5],
        "total_mv": [1e10] * 6,
        "float_mv": [8e9] * 6,
    })
    root = tmp_path / "data" / "daily" / "year=2026"
    root.mkdir(parents=True)
    df.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")
    return df


@pytest.fixture()
def _ctx(_bars):
    from lquant.research.dialect import jq_shim

    jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=_DAY,
                                   universe=["600519.SH"]))
    yield
    jq_shim.bind(None)


def _seed_financial(rows: list[dict]) -> None:
    df = pl.DataFrame(rows, schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    catalog.FinancialRepo().upsert(df)


# ---------- history ----------


class TestHistory:
    def test_single_field_returns_columns_by_symbol(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(3, "1d", "close", security_list=["600519.SH"])
        assert list(df.columns) == ["600519.SH"]
        assert len(df) == 3
        # 严格 <= current_dt，最后一天是 6/15
        assert df.index[-1] == _DAY
        assert df["600519.SH"].tolist()[-1] == 12.8

    def test_not_beyond_current_dt(self, _ctx):

        from lquant.research.dialect import jq_shim
        from lquant.research.dialect.jq_shim import history

        jq_shim.bind(jq_shim.JQContext(
            engine=None, trade_date=date(2026, 6, 12), universe=["600519.SH"]))
        df = history(10, "1d", "close", security_list=["600519.SH"])
        assert df.index[-1] == date(2026, 6, 12)
        assert all(d <= date(2026, 6, 12) for d in df.index)

    def test_default_security_list_is_universe(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(2, "1d", "close")
        assert list(df.columns) == ["600519.SH"]

    def test_avg_field(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(1, "1d", "avg")
        # 6/15: (12.5+13+12.2+12.8)/4
        assert abs(df["600519.SH"].iloc[-1] - (12.5 + 13.0 + 12.2 + 12.8) / 4) < 1e-9

    def test_money_field_maps_to_amount(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(1, "1d", "money")
        assert df["600519.SH"].iloc[-1] == 1e7

    def test_fq_pre_normalizes_by_latest_factor(self, _ctx):
        """前复权：p * f / f_latest；窗口最新因子 = 2。"""
        from lquant.research.dialect.jq_shim import history

        df = history(3, "1d", "close", security_list=["600519.SH"], fq="pre")
        # 6/13 close=11.8, factor=1, latest=2 → 5.9
        assert abs(df["600519.SH"].iloc[0] - 5.9) < 1e-9
        # 6/15 close=12.8, factor=2 → 12.8 不变
        assert abs(df["600519.SH"].iloc[-1] - 12.8) < 1e-9

    def test_fq_post_multiplies_factor(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(3, "1d", "close", security_list=["600519.SH"], fq="post")
        assert abs(df["600519.SH"].iloc[0] - 11.8) < 1e-9   # factor 1 → 原价
        assert abs(df["600519.SH"].iloc[-1] - 25.6) < 1e-9  # ×2

    def test_fq_none_returns_raw(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        df = history(3, "1d", "close", security_list=["600519.SH"], fq=None)
        assert df["600519.SH"].iloc[0] == 11.8

    def test_multi_field_single_security_layout(self, _ctx):
        """单证券多字段：列 = 字段（对齐聚宽布局规则）。"""
        from lquant.research.dialect.jq_shim import history

        df = history(2, "1d", ["close", "open"], security_list=["600519.SH"])
        assert list(df.columns) == ["close", "open"]
        assert df["close"].iloc[-1] == 12.8

    def test_invalid_unit_rejected(self, _ctx):
        from lquant.research.dialect.jq_shim import history

        with pytest.raises(ValueError, match="仅支持日频"):
            history(3, "5m", "close")


# ---------- get_fundamentals ----------


class TestGetFundamentalsPit:
    def test_future_pub_date_excluded(self, tmp_catalog, _ctx):
        """pub_date > current_dt 的报告绝不可见。"""
        _seed_financial([
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 100.0},
            {"symbol": "600519.SH", "stat_date": date(2026, 6, 30),
             "pub_date": date(2026, 8, 20), "report_type": "2026Q2",
             "item": "profit.netProfit", "value": 999.0},  # 未公告
        ])
        from lquant.research.dialect.fundamentals import income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        df = get_fundamentals(query(income.net_profit), date=_DAY)
        assert len(df) == 1
        assert df["net_profit"].iloc[0] == 100.0

    def test_latest_stat_date_wins(self, tmp_catalog, _ctx):
        _seed_financial([
            {"symbol": "600519.SH", "stat_date": date(2025, 12, 31),
             "pub_date": date(2026, 3, 28), "report_type": "2025Q4",
             "item": "profit.netProfit", "value": 1.0},
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 2.0},
        ])
        from lquant.research.dialect.fundamentals import income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        df = get_fundamentals(query(income.net_profit), date=_DAY)
        assert df["net_profit"].iloc[0] == 2.0
        assert df["statDate"].iloc[0].date() == date(2026, 3, 31)

    def test_valuation_from_daily_bar(self, tmp_catalog, _ctx):
        from lquant.research.dialect.fundamentals import query, valuation
        from lquant.research.dialect.jq_shim import get_fundamentals

        df = get_fundamentals(
            query(valuation.pe_ratio, valuation.market_cap), date=_DAY)
        row = df.iloc[0]
        assert row["pe_ratio"] == 35.0
        assert row["market_cap"] == 1e10
        assert row["day"].date() == _DAY

    def test_filter_by_code_in(self, tmp_catalog, _ctx):
        _seed_financial([
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 1.0},
            {"symbol": "000001.SZ", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 2.0},
        ])
        from lquant.research.dialect.fundamentals import income, query, valuation
        from lquant.research.dialect.jq_shim import get_fundamentals

        q = query(income.net_profit, valuation.pe_ratio).filter(
            income.code.in_(["000001.SZ"]))
        df = get_fundamentals(q, date=_DAY)
        assert len(df) == 1
        assert df["code"].iloc[0] == "000001.SZ"

    def test_balance_and_cashflow_tables(self, tmp_catalog, _ctx):
        _seed_financial([
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "balance.totalAssets", "value": 3.0},
        ])
        from lquant.research.dialect.fundamentals import balance, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        df = get_fundamentals(query(balance.total_assets), date=_DAY)
        assert df["total_assets"].iloc[0] == 3.0

    def test_order_by_and_limit(self, tmp_catalog, _ctx):
        _seed_financial([
            {"symbol": "000001.SZ", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 2.0},
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 1.0},
        ])
        from lquant.research.dialect.fundamentals import income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        q = query(income.code, income.net_profit).order_by(
            income.net_profit, ascending=False).limit(1)
        df = get_fundamentals(q, date=_DAY)
        assert len(df) == 1
        assert df["code"].iloc[0] == "000001.SZ"

    def test_requires_context_without_date(self):
        from lquant.research.dialect.fundamentals import income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        with pytest.raises(RuntimeError, match="JQ 上下文未初始化"):
            get_fundamentals(query(income.net_profit))

    def test_revised_report_later_pub_date_wins(self, tmp_catalog, _ctx):
        """同报告期的修订公告：PIT 取后公告的那份。"""
        _seed_financial([
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 1.0},
            {"symbol": "600519.SH", "stat_date": date(2026, 3, 31),
             "pub_date": date(2026, 5, 10), "report_type": "2026Q1",
             "item": "profit.netProfit", "value": 1.5},
        ])
        from lquant.research.dialect.fundamentals import income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        df = get_fundamentals(query(income.net_profit), date=_DAY)
        assert df["net_profit"].iloc[0] == 1.5

    def test_mixed_financial_tables_rejected(self, tmp_catalog, _ctx):
        from lquant.research.dialect.fundamentals import balance, income, query
        from lquant.research.dialect.jq_shim import get_fundamentals

        with pytest.raises(ValueError, match="一次只能查一张财务表"):
            get_fundamentals(query(income.net_profit, balance.total_assets),
                             date=_DAY)

    def test_order_by_non_selected_column(self, tmp_catalog, _ctx):
        """按未选中列排序（聚宽惯用法：按 PE 排、显示营收）。"""
        from lquant.research.dialect.fundamentals import income, query, valuation
        from lquant.research.dialect.jq_shim import get_fundamentals

        q = query(income.net_profit).order_by(valuation.pe_ratio)
        df = get_fundamentals(q, date=_DAY)
        assert "pe_ratio" not in df.columns
        assert "net_profit" in df.columns

    def test_reserved_name_rejected(self, _ctx):
        from lquant.research.dialect.fundamentals import income, query

        with pytest.raises(ValueError, match="保留列名"):
            query(income.day)


class TestHistoryEdgeCases:
    def test_fq_missing_factor_row_keeps_raw_price(self, tmp_path, monkeypatch):
        """因子缺失的行保留原始价（不该把价格变 null）。"""
        df = pl.DataFrame({
            "symbol": ["600519.SH"] * 3,
            "trade_date": [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)],
            "open": [10.0, 10.5, 11.0],
            "high": [10.5, 11.0, 11.5],
            "low": [9.8, 10.2, 10.7],
            "close": [10.2, 10.8, 11.2],
            "adj_factor": [1.0, None, 2.0],
        })
        root = tmp_path / "data" / "daily" / "year=2026"
        root.mkdir(parents=True)
        df.write_parquet(root / "part-0.parquet")
        monkeypatch.setattr("lquant.data.store.parquet._root",
                            lambda: tmp_path / "data")
        from lquant.research.dialect import jq_shim

        jq_shim.bind(jq_shim.JQContext(
            engine=None, trade_date=date(2026, 6, 12), universe=["600519.SH"]))
        try:
            out = jq_shim.history(3, "1d", "close",
                                  security_list=["600519.SH"], fq="post")
            # 6/10 ×1、6/11 缺因子→前向填充 1.0→原价、6/12 ×2
            assert out["600519.SH"].tolist() == [10.2, 10.8, 22.4]
        finally:
            jq_shim.bind(None)

    def test_empty_data_root_friendly_error(self, tmp_path, monkeypatch):
        """全新 checkout（日线根目录为空）：报可读错误，不是 polars 裸异常。"""
        monkeypatch.setattr("lquant.data.store.parquet._root",
                            lambda: tmp_path / "nonexistent")
        from lquant.research.dialect import jq_shim

        jq_shim.bind(jq_shim.JQContext(
            engine=None, trade_date=date(2026, 6, 12), universe=["600519.SH"]))
        try:
            with pytest.raises(ValueError, match="数据根目录为空"):
                jq_shim.history(3, "1d", "close")
        finally:
            jq_shim.bind(None)


class TestStrictAdj:
    """strict_adj：请求复权却缺因子时，绝不能静默按 factor=1.0 顶替。"""

    @staticmethod
    def _frame(*, with_factor_col: bool = True) -> pl.DataFrame:
        cols = {
            "symbol": ["600519.SH"] * 3,
            "trade_date": [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)],
            "open": [10.0, 10.5, 11.0],
            "high": [10.5, 11.0, 11.5],
            "low": [9.8, 10.2, 10.7],
            "close": [10.2, 10.8, 11.2],
        }
        if with_factor_col:
            cols["adj_factor"] = [None, None, 2.0]
        return pl.DataFrame(cols)

    def test_strict_raises_on_missing_rows(self):
        from lquant.research.dialect.jq_shim import apply_fq

        with pytest.raises(ValueError, match="缺有效 adj_factor"):
            apply_fq(self._frame(), "post", strict_adj=True, where="t")

    def test_strict_raises_when_factor_column_absent(self):
        """最危险的形态：请求了复权，但行情里根本没有因子列。"""
        from lquant.research.dialect.jq_shim import apply_fq

        with pytest.raises(ValueError, match="没有 adj_factor 列"):
            apply_fq(self._frame(with_factor_col=False), "pre",
                     strict_adj=True, where="t")

    def test_strict_raises_when_no_usable_factor_in_window(self):
        """整窗口因子都不可用 → pre 的归一基准无从谈起。"""
        from lquant.research.dialect.jq_shim import apply_fq

        df = pl.DataFrame({
            "symbol": ["600519.SH"] * 2,
            "trade_date": [date(2026, 6, 10), date(2026, 6, 11)],
            "close": [10.0, 11.0],
            "adj_factor": [None, None],
        })
        with pytest.raises(ValueError, match="没有任何有效 adj_factor"):
            apply_fq(df, "pre", strict_adj=True, where="t")

    def test_lenient_warns_instead_of_silence(self):
        """默认口径不变（缺因子退回原始价），但必须留下 warning —— 不再无声。"""
        from loguru import logger

        from lquant.research.dialect.jq_shim import apply_fq

        seen: list[str] = []
        sink_id = logger.add(lambda m: seen.append(m), level="WARNING")
        try:
            out = apply_fq(self._frame(), "post", strict_adj=False, where="t")
        finally:
            logger.remove(sink_id)
        assert out["close"].to_list() == [10.2, 10.8, 22.4]
        assert any("缺有效 adj_factor" in m for m in seen)

    def test_attribute_history_honors_fq(self, tmp_path, monkeypatch):
        """attribute_history 历史上收了 fq 却从不复权（静默返回原始价）。"""
        df = pl.DataFrame({
            "symbol": ["600519.SH"] * 3,
            "trade_date": [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)],
            "open": [10.0, 10.5, 11.0],
            "high": [10.5, 11.0, 11.5],
            "low": [9.8, 10.2, 10.7],
            "close": [10.2, 10.8, 11.2],
            "adj_factor": [1.0, 1.0, 2.0],
        })
        root = tmp_path / "data" / "daily" / "year=2026"
        root.mkdir(parents=True)
        df.write_parquet(root / "part-0.parquet")
        monkeypatch.setattr("lquant.data.store.parquet._root",
                            lambda: tmp_path / "data")
        from lquant.research.dialect import jq_shim

        jq_shim.bind(jq_shim.JQContext(
            engine=None, trade_date=date(2026, 6, 11), universe=["600519.SH"]))
        try:
            raw = jq_shim.attribute_history("600519.SH", 2, fields=("close",),
                                            fq=None, strict_adj=True)
            pre = jq_shim.attribute_history("600519.SH", 2, fields=("close",),
                                            fq="pre", strict_adj=True)
            post = jq_shim.attribute_history("600519.SH", 2, fields=("close",),
                                             fq="post", strict_adj=True)
            assert raw["close"].tolist() == [10.2, 10.8]
            # pre：除以窗口内最新因子 1.0 → 与原始价一致
            assert pre["close"].tolist() == [10.2, 10.8]
            # post：× 当日因子
            assert post["close"].tolist() == [10.2, 10.8]
        finally:
            jq_shim.bind(None)
