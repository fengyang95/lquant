"""指数成分同步（tushare index_weight → index_cons 快照）测试。离线跑。"""

from __future__ import annotations

from datetime import date

import polars as pl


def _fake_provider(monkeypatch, indexes: dict[str, str] | None = None) -> None:
    """monkeypatch TushareProvider._call；indexes=None → 恒空返回（测空响应）。"""

    def fake_call(self, api_name: str, **kw: object) -> pl.DataFrame:
        if api_name != "index_weight":
            raise AssertionError(f"unexpected api {api_name}")
        if indexes is None:
            return pl.DataFrame()
        code = kw["index_code"]
        return pl.DataFrame(
            {
                "index_code": [code] * 3,
                "con_code": ["600000.SH", "000001.SZ", "600519.SH"],
                "weight": [1.5, 2.0, 3.0],
                "trade_date": [indexes[code]] * 3,
            }
        )

    from lquant.data.providers import tushare as ts_mod

    monkeypatch.setattr(ts_mod.TushareProvider, "_call", fake_call)


def test_sync_index_cons(tmp_path, monkeypatch):
    from lquant.data.ingest import index_cons as mod

    _fake_provider(monkeypatch, {"000300.SH": "20260910", "000905.SH": "20260911"})
    monkeypatch.setattr(mod, "DEFAULT_INDEXES", ["000300.SH", "000905.SH"])

    summary = mod.sync_index_cons()
    assert summary["synced"] == 2
    assert summary["rows"] == 6
    assert summary["empty"] == []

    # 落库可读、eff_date 取返回数据里的最新 trade_date
    from lquant.core.db import writer
    from lquant.data.store.catalog import IndexConsRepo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)

    repo = IndexConsRepo()
    assert sorted(repo.latest_symbols("000300.SH")) == [
        "000001.SZ", "600000.SH", "600519.SH",
    ]
    assert repo.as_of("000905.SH", date(2026, 9, 12))["symbol"].to_list() == [
        "000001.SZ", "600000.SH", "600519.SH",
    ]

    # 幂等：重复同步不产生重复行（快照替换语义）
    summary2 = mod.sync_index_cons()
    assert summary2["rows"] == 6
    assert sorted(repo.latest_symbols("000300.SH")) == [
        "000001.SZ", "600000.SH", "600519.SH",
    ]


def test_sync_skips_empty_response(tmp_path, monkeypatch):
    from lquant.data.ingest import index_cons as mod

    _fake_provider(monkeypatch, None)
    monkeypatch.setattr(mod, "DEFAULT_INDEXES", ["000300.SH"])
    summary = mod.sync_index_cons()
    assert summary == {"synced": 0, "rows": 0, "empty": ["000300.SH"]}
