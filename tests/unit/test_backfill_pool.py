"""backfill_pool / Checkpoint.unmark 单元测试（不联网，mock provider）。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.data.ingest.checkpoint import Checkpoint


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """用 LQ_ROOT + cwd 隔离，而不是 patch get_settings。

    patch 模块属性会让测试期间被懒加载的模块（db/catalog 等）
    把 from-import 绑定永久绑到假对象上，泄漏到后续测试。
    """
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------- Checkpoint.unmark ----------


def test_checkpoint_unmark(fake_settings):
    cp = Checkpoint("t1")
    cp.mark(["a", "b"])
    cp.unmark(["a"])
    assert cp.is_done("b")
    assert not cp.is_done("a")


def test_checkpoint_unmark_persists(fake_settings):
    cp = Checkpoint("t2")
    cp.mark(["x", "y"])
    Checkpoint("t2").unmark(["x"])
    cp2 = Checkpoint("t2")
    assert cp2.is_done("y") and not cp2.is_done("x")


# ---------- Fake provider ----------


class FakeProvider:
    """按脚本逐次响应 daily_bars 调用。

    script: 每次调用一个动作
      - ("ok", n_rows)      成功，返回 n 行
      - ("raise", exc)      抛异常
    记录 calls: (symbols, start, end)
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[str], date, date | None]] = []

    def daily_bars(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        action = self.script.pop(0) if self.script else ("ok", 1)
        kind, val = action
        if kind == "raise":
            raise val
        return _df(symbols, val)


def _df(symbols: list[str], rows_per_sym: int) -> pl.DataFrame:
    if not rows_per_sym or not symbols:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": [s for s in symbols for _ in range(rows_per_sym)],
        "date": [date(2024, 1, 2)] * (len(symbols) * rows_per_sym),
        "close": [1.0] * (len(symbols) * rows_per_sym),
    })


@pytest.fixture
def no_lake(monkeypatch):
    """绕开真实血缘/入湖：_stamp 恒等，write_daily 只记账。"""
    written: list[pl.DataFrame] = []
    monkeypatch.setattr("lquant.data.ingest.daily._stamp", lambda df: df)
    monkeypatch.setattr(
        "lquant.data.ingest.daily.write_daily", written.append
    )
    return written


D = date(2024, 1, 5)


def _pool(n: int, end: date = D) -> list[tuple[str, date]]:
    return [(f"sh.6000{i:02d}", end) for i in range(n)]


# ---------- backfill_pool ----------


def test_happy_path(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    p = FakeProvider([("ok", 2)])
    res = backfill_pool(_pool(3), date(2024, 1, 1), provider=p)
    assert res["done"] == 3
    assert res["failed"] == []
    assert res["rows"] == 6
    assert res["early_stopped"] is False
    assert len(no_lake) == 1
    cp = Checkpoint("daily")
    assert all(cp.is_done(s) for s, _ in _pool(3))
    assert cp.meta["start"] == "2024-01-01"


def test_end_grouping(fake_settings, no_lake):
    """同批内 end 不同 → 按 end 分组拉取（daily_bars 只接受单一 end）。"""
    from lquant.data.ingest.daily import backfill_pool

    p = FakeProvider([("ok", 1), ("ok", 1)])
    pool = [("sh.600000", D), ("sh.600001", date(2023, 6, 30))]
    backfill_pool(pool, date(2024, 1, 1), provider=p)
    assert len(p.calls) == 2
    ends = {c[2] for c in p.calls}
    assert ends == {D, date(2023, 6, 30)}


def test_runtime_error_marks_failed(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    p = FakeProvider([("raise", RuntimeError("boom")), ("ok", 1)])
    pool = [("sh.600000", D), ("sh.600001", date(2023, 6, 30))]  # 不同 end → 分组
    res = backfill_pool(pool, date(2024, 1, 1), provider=p)
    assert res["done"] == 1
    assert [f["symbol"] for f in res["failed"]] == ["sh.600000"]
    assert res["failed"][0]["reason"]
    cp = Checkpoint("daily")
    assert not cp.is_done("sh.600000") and cp.is_done("sh.600001")


def test_timeout_shrinks_batch(fake_settings, no_lake):
    """TimeoutError → 缩到 20 只重试。"""
    from lquant.data.ingest.daily import backfill_pool

    syms = [f"sh.6{i:05d}" for i in range(50)]
    pool = [(s, D) for s in syms]

    calls = []

    class Shrinking:
        def daily_bars(self, symbols, start, end):
            calls.append(len(symbols))
            if len(symbols) > 20:
                raise TimeoutError("hang")
            return _df(symbols, 1)

    res = backfill_pool(pool, date(2024, 1, 1), provider=Shrinking())
    assert max(calls[:1]) == 50
    assert all(c <= 20 for c in calls[1:])
    assert res["done"] == 50
    assert res["failed"] == []


def test_early_stop_after_10_all_failed_batches(fake_settings, no_lake):
    """连续 10 批全失败 → 提前停，不再调 provider。"""
    from lquant.data.ingest.daily import backfill_pool

    p = FakeProvider([("raise", RuntimeError("down"))] * 30)
    # 250 只 ÷ batch_size=25 = 10 批，第 10 批失败即触发早停
    pool = _pool(250)
    res = backfill_pool(
        pool, date(2024, 1, 1), provider=p, batch_size=25
    )
    assert res["early_stopped"] is True
    assert len(p.calls) == 10
    assert res["done"] == 0
    assert len(res["failed"]) == 250
    cp = Checkpoint("daily")
    assert len(cp.done) == 0


def test_early_stop_resets_on_success(fake_settings, no_lake):
    """失败批被成功批打断 → 计数清零，不早停。"""
    from lquant.data.ingest.daily import backfill_pool

    script = [("raise", RuntimeError("x")), ("ok", 1)] * 15
    p = FakeProvider(script)
    res = backfill_pool(_pool(30 * 1), date(2024, 1, 1), provider=p, batch_size=1)
    assert res["early_stopped"] is False
    assert res["done"] == 15


def test_on_progress_frames(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    frames = []

    def cb(frame):
        frames.append(frame)
        if len(frames) == 2:
            raise ValueError("ui exploded")

    p = FakeProvider([("ok", 1), ("ok", 1)])
    res = backfill_pool(_pool(4), date(2024, 1, 1), provider=p, batch_size=2, on_progress=cb)
    assert res["done"] == 4
    assert len(frames) == 2
    for f in frames:
        assert set(f) >= {"done", "total", "failed", "rows"}
    assert frames[-1]["done"] == 4
    assert frames[-1]["total"] == 4


def test_on_progress_failed_frame_is_snapshot(fake_settings, no_lake):
    """帧里的 failed 是快照：后续批次的失败不能追溯改写已发出的帧。"""
    from lquant.data.ingest.daily import backfill_pool

    frames = []
    # 3 批各 1 只：第 1、3 批失败
    p = FakeProvider(
        [("raise", RuntimeError("x")), ("ok", 1), ("raise", RuntimeError("y"))]
    )
    pool = [(s, D) for s in ("a", "b", "c")]
    backfill_pool(pool, date(2024, 1, 1), provider=p, batch_size=1, on_progress=frames.append)
    assert [f["failed"] for f in frames] == [
        [{"symbol": "a", "reason": "x"}],  # 第 1 帧：累计失败 [a]
        [{"symbol": "a", "reason": "x"}],  # 第 2 帧：累计不变
        [{"symbol": "a", "reason": "x"}, {"symbol": "c", "reason": "y"}],  # 第 3 帧
    ]
    # 关键：帧 1 的 failed 不被帧 3 追溯改写
    assert frames[0]["failed"] == [{"symbol": "a", "reason": "x"}]


def test_on_progress_early_stop_flag(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    frames = []
    p = FakeProvider([("raise", RuntimeError("x"))] * 30)
    backfill_pool(
        _pool(50), date(2024, 1, 1), provider=p, batch_size=5,
        on_progress=frames.append,
    )
    assert frames[-1]["early_stopped"] is True
    assert all(f.get("early_stopped") is False for f in frames[:-1])


def test_skips_checkpoint_done(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    Checkpoint("daily").mark(["sh.600000"])
    p = FakeProvider([("ok", 1)])
    res = backfill_pool(_pool(2), date(2024, 1, 1), provider=p)
    assert res["done"] == 1
    assert [c[0] for c in p.calls] == [["sh.600001"]]


def test_empty_pool(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    res = backfill_pool([], date(2024, 1, 1), provider=FakeProvider([]))
    assert res == {"done": 0, "failed": [], "rows": 0, "early_stopped": False}


def test_empty_pool_keeps_meta(fake_settings, no_lake):
    """空跑不覆盖 checkpoint meta（避免 end=None 抹掉上次记录）。"""
    from lquant.data.ingest.daily import backfill_pool

    Checkpoint("daily").set_meta(start="2020-01-01", end="2020-12-31")
    backfill_pool([], date(2024, 1, 1), provider=FakeProvider([]))
    assert Checkpoint("daily").meta == {"start": "2020-01-01", "end": "2020-12-31"}


def test_quality_gate_fatal_blocks_batch(fake_settings, no_lake, monkeypatch):
    from lquant.core.errors import DataQualityError
    from lquant.data.ingest.daily import backfill_pool

    monkeypatch.setattr(
        "lquant.data.ingest.daily.write_daily",
        lambda df: (_ for _ in ()).throw(DataQualityError("rule", "bad")),
    )
    p = FakeProvider([("ok", 1)])
    res = backfill_pool(_pool(2), date(2024, 1, 1), provider=p)
    assert res["done"] == 0
    assert [f["symbol"] for f in res["failed"]] == ["sh.600000", "sh.600001"]


# ---------- backfill_daily 兼容 ----------


def test_backfill_daily_delegates(fake_settings, no_lake, monkeypatch):
    from lquant.data.ingest import daily as daily_mod

    captured = {}

    def fake_pool(pool, start, end=None, on_progress=None, **kw):
        captured["pool"] = pool
        captured["start"] = start
        captured["end"] = end
        return {"done": 7, "failed": [], "rows": 7, "early_stopped": False}

    monkeypatch.setattr(daily_mod, "backfill_pool", fake_pool)
    monkeypatch.setattr(
        "lquant.data.store.catalog.SecurityRepo.active_symbols",
        lambda self: ["sh.600000", "sz.000001"],
    )
    n = daily_mod.backfill_daily(full=True, start="2024-01-01", end="2024-01-31")
    assert n == 7
    assert captured["pool"] == [
        ("sh.600000", date(2024, 1, 31)),
        ("sz.000001", date(2024, 1, 31)),
    ]
    assert captured["start"] == date(2024, 1, 1)


def test_cp_name_isolation(fake_settings, no_lake):
    from lquant.data.ingest.daily import backfill_pool

    Checkpoint("daily").mark(["sh.600000"])
    p = FakeProvider([("ok", 1), ("ok", 1)])
    res = backfill_pool(
        [("sh.600000", D), ("sh.600001", D)], date(2024, 1, 1),
        provider=p, cp_name="task1",
    )
    assert res["done"] == 2
    assert Checkpoint("task1").done == {"sh.600000", "sh.600001"}
    assert Checkpoint("daily").done == {"sh.600000"}
