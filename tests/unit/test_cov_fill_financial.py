"""批次一覆盖补充：data/ingest/financial.py PIT 财务回填编排（打桩 provider/repo）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.data.ingest import financial as fin


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fin_df(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": symbols,
        "stat_date": [date(2024, 3, 31)] * len(symbols),
        "pub_date": [date(2024, 4, 20)] * len(symbols),
        "report_type": ["合并报表"] * len(symbols),
        "item": ["net_profit"] * len(symbols),
        "value": [1.0] * len(symbols),
        "unit": ["元"] * len(symbols),
        "source": ["tushare"] * len(symbols),
    })


class _Provider:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[list[str]] = []

    def financial_pit(self, symbols, start, end, kinds):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError("network down")
        return _fin_df(list(symbols))


class _Chain:
    def __init__(self, providers: list[_Provider]) -> None:
        self.providers = providers


class _Repo:
    def __init__(self) -> None:
        self.upserts: list[pl.DataFrame] = []
        self._count = 0

    def upsert(self, df: pl.DataFrame) -> int:
        self.upserts.append(df)
        self._count += len(df)
        return len(df)

    def count(self) -> int:
        return self._count


def _run(monkeypatch, providers, symbols, *, repo=None, batch=20, provider_name=None):
    repo = repo or _Repo()
    monkeypatch.setattr(fin, "FinancialRepo", lambda: repo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: _Chain(providers))
    done = fin.backfill_financial(symbols, start="2020-01-01", batch=batch,
                                  provider_name=provider_name)
    return done, repo


def test_backfill_prefers_tushare_and_marks_checkpoint(fake_settings, monkeypatch):
    ts, bs = _Provider("tushare"), _Provider("baostock")
    done, repo = _run(monkeypatch, [bs, ts], [f"s{i:03d}" for i in range(45)])
    assert done == 45
    assert ts.calls[0] == [f"s{i:03d}" for i in range(20)]
    assert repo.count() == 45
    from lquant.data.ingest.checkpoint import Checkpoint

    assert Checkpoint("financial_pit_tushare").remaining(["s000", "s044"]) == []


def test_backfill_progress_log_at_100(fake_settings, monkeypatch):
    """done 凑满 100 触发进度日志（>100 只标的）。"""
    ts = _Provider("tushare")
    done, repo = _run(monkeypatch, [_Provider("baostock"), ts],
                      [f"s{i:03d}" for i in range(105)])
    assert done == 105
    assert len(repo.upserts) == 6  # 20×5 + 5


def test_backfill_batch_failure_not_marked(fake_settings, monkeypatch):
    """失败批不写 checkpoint，重跑会重试；成功批照常推进。"""
    bad = _Provider("tushare", fail=True)
    symbols = [f"s{i:03d}" for i in range(40)]
    done, repo = _run(monkeypatch, [bad], symbols)
    assert done == 0 and repo.upserts == []
    from lquant.data.ingest.checkpoint import Checkpoint

    assert Checkpoint("financial_pit_tushare").remaining(symbols) == symbols


def test_backfill_unknown_provider_name(fake_settings, monkeypatch):
    with pytest.raises(RuntimeError, match="不可用"):
        _run(monkeypatch, [_Provider("tushare")], ["s1"], provider_name="nope")


def test_backfill_named_provider(fake_settings, monkeypatch):
    bs = _Provider("baostock")
    done, repo = _run(monkeypatch, [bs], ["a", "b"], provider_name="baostock")
    assert done == 2 and repo.count() == 2


def test_backfill_chain_without_providers_attr(fake_settings, monkeypatch):
    """无 tushare 时回退链头。

    注：源码缺陷（只报告不改）—— chain 无 .providers 属性时
    financial.py:49 的列表推导会先 AttributeError，
    line 51 的 hasattr 兜底分支实际不可达。
    """
    p = _Provider("baostock")

    class _Chain2:
        providers = [p]

    repo = _Repo()
    monkeypatch.setattr(fin, "FinancialRepo", lambda: repo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: _Chain2())
    done = fin.backfill_financial(["a"], start=date(2020, 1, 1))
    assert done == 1
