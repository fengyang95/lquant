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


def _run(monkeypatch, providers, symbols, *, repo=None, batch=20, provider_name=None,
         start="2020-01-01", end=None):
    repo = repo or _Repo()
    monkeypatch.setattr(fin, "FinancialRepo", lambda: repo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: _Chain(providers))
    out = fin.backfill_financial(symbols, start=start, end=end, batch=batch,
                                 provider_name=provider_name)
    return out, repo


def test_backfill_prefers_tushare_and_marks_checkpoint(fake_settings, monkeypatch):
    ts, bs = _Provider("tushare"), _Provider("baostock")
    out, repo = _run(monkeypatch, [bs, ts], [f"s{i:03d}" for i in range(45)])
    assert out["done"] == 45
    assert out["skipped_covered"] == 0
    assert ts.calls[0] == [f"s{i:03d}" for i in range(20)]
    assert repo.count() == 45
    from lquant.data.ingest.checkpoint import Checkpoint

    assert Checkpoint("financial_pit_tushare").remaining(["s000", "s044"]) == []


def test_backfill_progress_log_at_100(fake_settings, monkeypatch):
    """done 凑满 100 触发进度日志（>100 只标的）。"""
    ts = _Provider("tushare")
    out, repo = _run(monkeypatch, [_Provider("baostock"), ts],
                     [f"s{i:03d}" for i in range(105)])
    assert out["done"] == 105
    assert len(repo.upserts) == 6  # 20×5 + 5


def test_backfill_batch_failure_not_marked(fake_settings, monkeypatch):
    """失败批不写 checkpoint，重跑会重试；成功批照常推进。"""
    bad = _Provider("tushare", fail=True)
    symbols = [f"s{i:03d}" for i in range(40)]
    out, repo = _run(monkeypatch, [bad], symbols)
    assert out["done"] == 0 and repo.upserts == []
    from lquant.data.ingest.checkpoint import Checkpoint

    assert Checkpoint("financial_pit_tushare").remaining(symbols) == symbols


def test_backfill_unknown_provider_name(fake_settings, monkeypatch):
    with pytest.raises(RuntimeError, match="不可用"):
        _run(monkeypatch, [_Provider("tushare")], ["s1"], provider_name="nope")


def test_backfill_named_provider(fake_settings, monkeypatch):
    bs = _Provider("baostock")
    out, repo = _run(monkeypatch, [bs], ["a", "b"], provider_name="baostock")
    assert out["done"] == 2 and repo.count() == 2


def test_backfill_chain_without_providers_attr(fake_settings, monkeypatch):
    """无 tushare 时回退链头。

    注：源码缺陷（只报告不改）—— chain 无 .providers 属性时
    financial.py 的列表推导会先 AttributeError，
    后面的 hasattr 兜底分支实际不可达。
    """
    p = _Provider("baostock")

    class _Chain2:
        providers = [p]

    repo = _Repo()
    monkeypatch.setattr(fin, "FinancialRepo", lambda: repo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: _Chain2())
    out = fin.backfill_financial(["a"], start=date(2020, 1, 1))
    assert out["done"] == 1


# ------------------------------------------------ 窗口语义（2026-09-18 修）


def test_full_backfill_does_not_starve_incremental_window(fake_settings, monkeypatch):
    """全市场回填后，另一窗口的增量作业必须照跑（本缺陷的核心回归）。

    旧实现按 symbol 记 done：2016 起全量回填把 5898 只标成 done 之后，
    每日 90 天窗口的作业 remaining 恒为空 —— 新公告永远进不来、
    作业还报 ok。窗口变了就必须重新拉。
    """
    ts = _Provider("tushare")
    syms = [f"s{i:03d}" for i in range(10)]
    _run(monkeypatch, [ts], syms, start="2016-01-01", end="2026-09-14")
    assert len(ts.calls) == 1

    # 同窗口重跑：全部命中覆盖区间 → 一次源调用都没有
    ts2 = _Provider("tushare")
    out, _ = _run(monkeypatch, [ts2], syms, start="2016-01-01", end="2026-09-14")
    assert ts2.calls == [] and out["done"] == 0
    assert out["skipped_covered"] == 10

    # end 前进一天：旧覆盖区间不再包含新窗口 → 必须以增量段重拉（不是空转）
    ts3 = _Provider("tushare")
    out3, _ = _run(monkeypatch, [ts3], syms, start="2026-09-01", end="2026-09-18")
    assert len(ts3.calls) == 1, "窗口前移后必须真的去拉，不能静默跳过"
    assert out3["skipped_covered"] == 0


def test_incremental_only_requests_uncovered_tail(fake_settings, monkeypatch):
    """已覆盖到 09-14、请求到 09-18 → 只拉 09-15~09-18 这一段。"""
    ts = _Provider("tushare")
    syms = ["a", "b"]
    _run(monkeypatch, [ts], syms, start="2026-06-01", end="2026-09-14")
    seen: list[tuple] = []
    orig = ts.financial_pit

    def spy(symbols, start, end, kinds):
        seen.append((list(symbols), start, end))
        return orig(symbols, start, end, kinds)

    ts.financial_pit = spy  # type: ignore[method-assign]
    out, _ = _run(monkeypatch, [ts], syms, start="2026-06-01", end="2026-09-18")
    assert seen == [(["a", "b"], date(2026, 9, 15), date(2026, 9, 18))]
    assert out["done"] == 2


def test_wider_request_window_repulls_from_start(fake_settings, monkeypatch):
    """请求窗口比已覆盖区间更早 → 从窗口起点整段重拉（宁可重复不漏）。"""
    ts = _Provider("tushare")
    _run(monkeypatch, [ts], ["a"], start="2026-06-01", end="2026-09-14")
    seen: list[tuple] = []
    orig = ts.financial_pit

    def spy(symbols, start, end, kinds):
        seen.append((start, end))
        return orig(symbols, start, end, kinds)

    ts.financial_pit = spy  # type: ignore[method-assign]
    _run(monkeypatch, [ts], ["a"], start="2026-01-01", end="2026-09-18")
    assert seen == [(date(2026, 1, 1), date(2026, 9, 18))]


def test_todo_windows_reversed_and_covered(fake_settings):
    """窗口记账的边界：请求窗口反了不产生增量段；已覆盖窗口整段跳过。"""
    from lquant.data.ingest.checkpoint import Checkpoint

    cp = Checkpoint("financial_pit_tushare")
    # start > end：既不生成增量段，也不能造出一个 (start, end) 坏区间
    assert fin._todo_windows(cp, ["a", "b"], date(2026, 9, 18),
                             date(2026, 9, 1)) == {}

    cp2 = Checkpoint("financial_pit_tushare")
    cp2.record_coverage(["a"], date(2026, 6, 1), date(2026, 9, 18))
    assert fin._todo_windows(cp2, ["a"], date(2026, 6, 1), date(2026, 9, 18)) == {}
    # 只有尾段未覆盖 → 增量从已覆盖区间末尾的次日开始
    assert fin._todo_windows(cp2, ["a"], date(2026, 6, 1),
                             date(2026, 12, 31)) == {date(2026, 9, 19): ["a"]}


def test_backfill_accepts_bare_provider_without_chain(fake_settings, monkeypatch):
    """get_provider() 返回的不是链（没有 .providers）→ 直接当单源用。

    checkpoint 键仍要带源名 —— 换源重跑不该被另一个源的记账误跳过。
    """
    from lquant.data.ingest.checkpoint import Checkpoint

    ts = _Provider("tushare")
    repo = _Repo()
    monkeypatch.setattr(fin, "FinancialRepo", lambda: repo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: ts)
    out = fin.backfill_financial(["a"], start="2026-06-01", end="2026-09-18")
    assert out["done"] == 1 and repo.count() == 1
    assert Checkpoint("financial_pit_tushare").covers(
        "a", date(2026, 6, 1), date(2026, 9, 18))


def test_legacy_checkpoint_without_coverage_is_repulled(fake_settings, monkeypatch):
    """老 checkpoint 只有 done、没有覆盖区间 → 视为未覆盖，按窗口重拉。

    这是真实数据的自愈路径：financial_pit_tushare.json 里有 5898 个
    done-without-window 条目，修完必须能让它们重新拉一遍。
    """
    from lquant.data.ingest.checkpoint import Checkpoint

    cp = Checkpoint("financial_pit_tushare")
    cp.mark(["a", "b"])
    assert cp.covered_window("a") is None
    ts = _Provider("tushare")
    out, _ = _run(monkeypatch, [ts], ["a", "b"], start="2026-06-01", end="2026-09-18")
    assert out["done"] == 2 and len(ts.calls) == 1
    assert Checkpoint("financial_pit_tushare").covers("a", date(2026, 6, 1),
                                                      date(2026, 9, 18))


def test_checkpoint_window_ledger_guards(fake_settings):
    """窗口记账的四条防守：半窗口报错 / 反区间不记 / 脏条目跳过 / clear 清覆盖。

    每条失守都会制造「静默缺口」：checkpoint 说覆盖了、数据其实没有；
    或者反过来——重置后回填以为自己覆盖过而整段跳过。
    """
    import json

    from lquant.data.ingest.checkpoint import Checkpoint

    cp = Checkpoint("financial_pit_tushare")
    # ① 只给一端必须明确报错：静默回落到 done 语义会把「没窗口」当「已完成」
    with pytest.raises(ValueError, match="同时给或同时不给"):
        cp.remaining(["a"], start=date(2026, 1, 1))
    with pytest.raises(ValueError, match="同时给或同时不给"):
        cp.remaining(["a"], end=date(2026, 1, 1))

    # ② 反区间（end < start）不记覆盖、也不算 done，该标的仍算未完成
    cp.record_coverage(["a"], date(2026, 9, 18), date(2026, 9, 1))
    assert cp.covered_window("a") is None and cp.done == set()
    assert cp.remaining(["a"], start=date(2026, 1, 1),
                        end=date(2026, 9, 1)) == ["a"]

    # ③ 文件里的脏条目（手改 / 半写）跳过，不影响同键的合法区间
    cp.record_coverage(["b"], date(2026, 1, 1), date(2026, 6, 30))
    raw = json.loads(cp.path.read_text(encoding="utf-8"))
    raw["coverage"]["b"] += [["not-a-date", "2026-07-01"], ["2026-07-01"]]
    cp.path.write_text(json.dumps(raw), encoding="utf-8")
    cp2 = Checkpoint("financial_pit_tushare")
    assert cp2.covered_window("b") == (date(2026, 1, 1), date(2026, 6, 30))
    assert cp2.covers("b", date(2026, 1, 1), date(2026, 6, 30)) is True

    # ④ clear 必须连覆盖区间一起清：只清 done 会让回填整段跳过（静默缺口）
    cp2.clear()
    assert cp2.done == set() and cp2.covered_window("b") is None
    assert Checkpoint("financial_pit_tushare").covered_window("b") is None
    assert cp2.remaining(["b"], start=date(2026, 1, 1),
                         end=date(2026, 6, 30)) == ["b"]

