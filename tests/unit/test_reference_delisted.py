"""reference.sync_delisted 退市股名单同步单元测试（mock akshare，不联网）。

隔离方式沿用 test_data_tasks：LQ_ROOT env + chdir + cache_clear。
"""

from __future__ import annotations

import polars as pl
import pytest

from lquant.data.ingest import reference as reference_mod


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_security(rows: list[tuple[str, str, object, object]]) -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security ("
            "symbol VARCHAR PRIMARY KEY, name VARCHAR, sec_type VARCHAR, "
            "board VARCHAR, list_date DATE, delist_date DATE, is_st BOOLEAN, "
            "source VARCHAR, updated_at TIMESTAMP)"
        )
        for sym, st, ld, dd in rows:
            con.execute(
                "INSERT INTO security VALUES (?, NULL, ?, NULL, ?, ?, NULL, NULL, NULL)",
                [sym, st, ld, dd],
            )


class _FakeAk:
    """akshare 沪深退市接口替身：列名与真实返回一致。"""

    @staticmethod
    def stock_info_sh_delist(symbol: str = "全部") -> pl.DataFrame:
        return pl.DataFrame(
            {
                "公司代码": ["600001", "600002"],
                "公司简称": ["邯郸钢铁", "齐鲁退市"],
                "上市日期": ["1998-01-22", "1998-04-08"],
                "暂停上市日期": ["2009-12-29", "2006-04-24"],
            }
        )

    @staticmethod
    def stock_info_sz_delist(symbol: str = "终止上市公司") -> pl.DataFrame:
        return pl.DataFrame(
            {
                "证券代码": ["000003", "000004"],
                "证券简称": ["PT金田Ａ", "国华退"],
                "上市日期": ["1991-01-14", "1990-12-01"],
                "终止上市日期": ["2002-06-14", "2026-07-14"],
            }
        )


@pytest.fixture
def fake_ak(monkeypatch):
    monkeypatch.setattr(reference_mod, "_ak_delist_module", lambda: _FakeAk)


def test_sync_delisted_upserts_all(fake_settings, fake_ak):
    _seed_security([])
    n = reference_mod.sync_delisted()
    assert n == 4
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT symbol, name, list_date, delist_date, sec_type FROM security ORDER BY symbol"
        ).fetchall()
    by_sym = {r[0]: r for r in rows}
    assert by_sym["600001.SH"][1] == "邯郸钢铁"
    assert str(by_sym["600001.SH"][3]) == "2009-12-29"
    assert str(by_sym["000003.SZ"][3]) == "2002-06-14"
    assert by_sym["600001.SH"][4] == "stock"


def test_sync_delisted_preserves_existing_details(fake_settings, fake_ak):
    """库里已有的 list_date/delist_date 不被整行覆盖冲掉。"""
    from datetime import date

    _seed_security([("600001.SH", "stock", date(1998, 1, 22), date(2009, 1, 15))])
    reference_mod.sync_delisted()
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute(
            "SELECT name, list_date, delist_date FROM security WHERE symbol='600001.SH'"
        ).fetchone()
    assert row[0] == "邯郸钢铁"
    # 库里已有 delist_date → 保留旧值（不被官方名单覆盖成另一口径）
    assert str(row[2]) == "2009-01-15"


def test_sync_delisted_skips_unparseable_codes(fake_settings, monkeypatch):
    """B股等 parse_symbol 不认识的代码只丢行，不能拖垮整张退市表（真实故障点）。"""

    class _BShareAk:
        @staticmethod
        def stock_info_sh_delist(symbol: str = "全部") -> pl.DataFrame:
            return pl.DataFrame()

        @staticmethod
        def stock_info_sz_delist(symbol: str = "终止上市公司") -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "证券代码": ["000003", "200002"],  # 后者是深市 B 股
                    "证券简称": ["PT金田Ａ", "PT金田B"],
                    "上市日期": ["1991-01-14", "1991-01-14"],
                    "终止上市日期": ["2002-06-14", "2002-06-14"],
                }
            )

    monkeypatch.setattr(reference_mod, "_ak_delist_module", lambda: _BShareAk)
    _seed_security([])
    n = reference_mod.sync_delisted()
    assert n == 1  # 000003 入库，200002 被跳过
    from lquant.core.db import reader

    with reader() as con:
        syms = [r[0] for r in con.execute("SELECT symbol FROM security").fetchall()]
    assert syms == ["000003.SZ"]


def test_sync_delisted_empty_source(fake_settings, monkeypatch):
    class _Empty:
        @staticmethod
        def stock_info_sh_delist(symbol: str = "全部") -> pl.DataFrame:
            return pl.DataFrame()

        @staticmethod
        def stock_info_sz_delist(symbol: str = "终止上市公司") -> pl.DataFrame:
            return pl.DataFrame()

    monkeypatch.setattr(reference_mod, "_ak_delist_module", lambda: _Empty)
    _seed_security([])
    assert reference_mod.sync_delisted() == 0


def test_sync_reference_includes_delisted(fake_settings, fake_ak, monkeypatch):
    """sync_reference 串入退市名单，且在 details 之前（慢路径要能遍历到退市股）。"""
    order: list[str] = []
    monkeypatch.setattr(reference_mod, "sync_calendar", lambda *a, **k: order.append("cal") or 1)
    monkeypatch.setattr(reference_mod, "sync_securities", lambda *a, **k: order.append("sec") or 1)
    monkeypatch.setattr(reference_mod, "sync_delisted", lambda *a, **k: order.append("delist") or 1)
    monkeypatch.setattr(
        reference_mod, "sync_security_details", lambda *a, **k: order.append("details") or 0
    )
    out = reference_mod.sync_reference()
    assert order == ["cal", "sec", "delist", "details"]
    assert out["delisted"] == 1


def test_sync_reference_continues_on_step_failure(
    fake_settings,
    fake_ak,
    monkeypatch,
):
    """单步失败不中断整体：日历挂了，退市名单仍要入库；最后汇总报错。"""

    def boom(*a, **k):
        raise RuntimeError("baostock 挂起")

    order: list[str] = []
    monkeypatch.setattr(reference_mod, "sync_calendar", boom)
    monkeypatch.setattr(reference_mod, "sync_securities", lambda *a, **k: order.append("sec") or 5)
    monkeypatch.setattr(reference_mod, "sync_delisted", lambda *a, **k: order.append("delist") or 4)
    monkeypatch.setattr(
        reference_mod, "sync_security_details", lambda *a, **k: order.append("details") or 0
    )
    with pytest.raises(RuntimeError, match="calendar"):
        reference_mod.sync_reference()
    assert order == ["sec", "delist", "details"]


def test_sync_securities_calls_provider_without_args(fake_settings, monkeypatch):
    """回归：sync_securities 不能把 day 传给 provider.securities() ——
    FallbackProvider.securities() 不收参数，传了会 TypeError。"""
    calls: list[tuple] = []

    class _P:
        def securities(self):
            calls.append(())
            return pl.DataFrame(
                {
                    "symbol": ["600001.SH"],
                    "name": ["x"],
                    "sec_type": ["stock"],
                }
            )

    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: _P())
    _seed_security([])
    assert reference_mod.sync_securities() == 1
    assert calls == [()]


def test_merge_preserves_old_dates_when_new_frame_has_no_date_cols(fake_settings):
    """回归：baostock 快路径清单不含 list_date/delist_date 列。

    旧实现无条件引用 join 后的 *_right 列 → ColumnNotFoundError，
    全市场 reference 同步必炸。不带日期列时应直接保留库内旧值。
    """
    _seed_security([("600001.SH", "stock", "1998-01-22", None)])
    df = pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH"],
            "name": ["x", "y"],
            "sec_type": ["stock", "stock"],
            "board": ["main", "main"],
        }
    )
    out = reference_mod._merge_existing_details(df)
    assert set(out.columns) >= {"symbol", "list_date", "delist_date"}
    row = out.filter(pl.col("symbol") == "600001.SH")
    assert row["list_date"][0] is not None and str(row["list_date"][0]) == "1998-01-22"
    assert row["delist_date"][0] is None
    # 库里没有的新标的：日期列为 NULL，不炸
    new_row = out.filter(pl.col("symbol") == "600002.SH")
    assert new_row["list_date"][0] is None


def test_merge_coalesce_keeps_new_over_old_by_default(fake_settings):
    """默认（keep_existing=False）：新值优先，空缺回落旧值。"""
    _seed_security([("600001.SH", "stock", "1990-01-01", None)])
    df = pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "name": ["x"],
            "sec_type": ["stock"],
            "board": ["main"],
            "list_date": [None],
        }
    )
    out = reference_mod._merge_existing_details(df)
    # 新值为 NULL → 回落旧值
    assert str(out["list_date"][0]) == "1990-01-01"


def test_merge_keep_existing_prefers_old(fake_settings):
    """keep_existing=True（退市官方口径）：旧值非空则完全保留。"""
    _seed_security([("600001.SH", "stock", "1990-01-01", None)])
    df = pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "name": ["x"],
            "sec_type": ["stock"],
            "board": ["main"],
            "list_date": ["1998-01-22"],
            "delist_date": ["2009-12-29"],
        }
    )
    out = reference_mod._merge_existing_details(df, keep_existing=True)
    assert str(out["list_date"][0]) == "1990-01-01"
    assert str(out["delist_date"][0]) == "2009-12-29"  # 旧值为空 → 补新值


def test_sync_security_details_keeps_source_and_updated_at(fake_settings, monkeypatch):
    """回归：details 落库时列存在性误用原始 df 判断，source/updated_at
    被自己的 select 过滤 → INSERT OR REPLACE 把已入库行的
    board/is_st/source/updated_at 冲成 NULL（实测 6920 行受害）。"""
    _seed_security([("000002.SH", "stock", None, None)])
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "UPDATE security SET board='main', is_st=false, "
            "source='baostock', updated_at='2026-01-01 00:00:00' WHERE symbol='000002.SH'"
        )

    import polars as pl

    class _P:
        def security_details(self, symbols):
            return pl.DataFrame(
                [
                    {
                        "symbol": "000002.SH",
                        "name": "上证A股指数",
                        "list_date": None,
                        "delist_date": None,
                        "sec_type": "index",
                    }
                ]
            )

    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: _P())
    n = reference_mod.sync_security_details()
    assert n == 1
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute(
            "SELECT sec_type, source, board, is_st FROM security WHERE symbol='000002.SH'"
        ).fetchone()
    assert row[0] == "index"
    assert row[1] is not None and row[1] != ""  # source 必须保住
    assert row[2] == "main"  # 未提供的列不能被冲成 NULL
    assert row[3] == False  # noqa: E712  is_st 同理
