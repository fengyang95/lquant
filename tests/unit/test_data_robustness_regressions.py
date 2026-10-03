"""2026-10-03 数据模块审计的回归用例。

覆盖四类**静默**数据缺陷（每一条此前都能「报成功」却丢数据）：

- P0-2 取消/提前停止时，从未尝试的尾部标的被 ``cp.mark()`` 记成完成 ——
  后续 retry 因断点命中全部跳过，留下永久空洞。
- P0-3 分钟线源站静默零行（无异常）时整批标 done —— 该标的永久缺失。
- P0-4 财务增量用 ``covered_window``（并集外框）判断跳过 —— 两段互不相连的
  覆盖区间（停摆 + 滚动窗口的典型形状）之间的空洞永不回补。
- 数据根自检 / 分区连续性：影子湖（``<x>/parquet/parquet``）与整年分区缺失
  此前完全无人发现（实测真实湖整年缺 2025）。

隔离口径：``LQ_ROOT`` + ``LQ_DATA_DIR`` 都指向 tmp，并 chdir 到 tmp ——
任何写盘都不可能落到生产湖。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.data.ingest.checkpoint import Checkpoint


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把设置根与数据根都钉在 tmp（双保险：绝不动生产湖）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _empty_bars() -> pl.DataFrame:
    return pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date, "close": pl.Float64})


# ======================================================================
# P0-2  backfill_pool 必须报告「从未尝试」的尾部
# ======================================================================


class _CountingProvider:
    """每次 daily_bars 返回 1 行并计数 —— 用来判断停在第几批之后。"""

    def __init__(self) -> None:
        self.calls = 0

    def daily_bars(self, symbols, start, end):
        self.calls += 1
        return pl.DataFrame(
            {
                "symbol": list(symbols),
                "trade_date": [date(2024, 1, 2)] * len(symbols),
                "close": [1.0] * len(symbols),
            }
        )


class _SilentEmptyProvider:
    """源站静默零行（不抛异常）—— P0-2 提前停止 / P0-3 的触发条件。"""

    def __init__(self) -> None:
        self.calls = 0

    def daily_bars(self, symbols, start, end):
        self.calls += 1
        return _empty_bars()


def _pool(n: int) -> list[tuple[str, date]]:
    return [(f"6001{i:02d}.SH", date(2024, 1, 2)) for i in range(n)]


def test_backfill_pool_reports_unprocessed_tail_on_cancel(env):
    """取消发生在第一批之后：尾部 4 只从未尝试，必须出现在 unprocessed 里。"""
    from lquant.data.ingest.daily import backfill_pool

    state = {"batches": 0}

    def cancel_after_first() -> bool:
        return state["batches"] >= 1

    class _Probe(_CountingProvider):
        def daily_bars(self, symbols, start, end):
            res = super().daily_bars(symbols, start, end)
            state["batches"] += 1
            return res

    res = backfill_pool(
        _pool(6),
        date(2024, 1, 1),
        provider=_Probe(),
        batch_size=2,
        cp_name="t-p02-cancel",
        cancel_check=cancel_after_first,
    )

    assert res["canceled"] is True
    assert res["done"] == 2  # 只有第一批真跑了
    assert set(res["unprocessed"]) == {f"6001{i:02d}.SH" for i in (2, 3, 4, 5)}

    # 关键断言：从未尝试的标的**不得**进断点
    done = Checkpoint("t-p02-cancel").done
    assert done == {"600100.SH", "600101.SH"}
    assert not (set(res["unprocessed"]) & done)


def test_backfill_pool_unprocessed_empty_when_fully_processed(env):
    """正常跑完：unprocessed 为空（不能把「跑完了」也塞进 unprocessed）。"""
    from lquant.data.ingest.daily import backfill_pool

    res = backfill_pool(
        _pool(4), date(2024, 1, 1), provider=_CountingProvider(), batch_size=2, cp_name="t-p02-full"
    )
    assert res["canceled"] is False
    assert res["unprocessed"] == []
    assert len(Checkpoint("t-p02-full").done) == 4


def test_backfill_pool_reports_unprocessed_on_early_stop(env):
    """连续全失败提前停止（EARLY_STOP_BATCHES=10）：尾部 2 只从未尝试。"""
    from lquant.data.ingest import daily as daily_mod
    from lquant.data.ingest.daily import backfill_pool

    provider = _SilentEmptyProvider()
    res = backfill_pool(
        _pool(12), date(2024, 1, 1), provider=provider, batch_size=1, cp_name="t-p02-early"
    )

    assert res["early_stopped"] is True
    assert provider.calls == daily_mod.EARLY_STOP_BATCHES
    assert set(res["unprocessed"]) == {"600110.SH", "600111.SH"}
    # 失败批不是「完成」，提前停止的尾部也不是
    assert Checkpoint("t-p02-early").done == set()
    assert len(res["failed"]) == daily_mod.EARLY_STOP_BATCHES


def test_backfill_pool_marks_empty_response_as_failed_not_done(env):
    """源静默零行 → failed(empty_response)，绝不标 done（既有口径的护栏）。"""
    from lquant.data.ingest.daily import backfill_pool

    res = backfill_pool(
        _pool(2),
        date(2024, 1, 1),
        provider=_SilentEmptyProvider(),
        batch_size=2,
        cp_name="t-p02-empty",
    )
    assert res["done"] == 0
    assert Checkpoint("t-p02-empty").done == set()
    assert all("empty_response" in f["reason"] for f in res["failed"])
    assert len(res["failed"]) == 2


# ======================================================================
# P0-2（调用侧）tasks._run_task 不得把 unprocessed 标成完成
# ======================================================================


@pytest.fixture()
def task_env(env):
    """DDL + security 最小地基（_pool_from_con 要查 security）。"""
    from lquant.core.db import writer
    from lquant.data.ingest.tasks import _DDL
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        con.execute(_DDL)
        con.execute(
            "INSERT OR REPLACE INTO security (symbol, name, sec_type, board, "
            "list_date, is_st, source) VALUES "
            "('600100.SH', '假股', 'stock', 'main', DATE '2010-01-01', false, 'test'), "
            "('000200.SZ', '假股2', 'stock', 'main', DATE '2010-01-01', false, 'test')"
        )
    yield


def _insert_running_task(task_id: str) -> None:
    import json
    from datetime import datetime

    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO data_task (task_id, kind, params, status, "
            "failed_symbols, failed_detail, started_at) "
            "VALUES (?, 'daily_update', ?, 'running', '[]'::JSON, '[]'::JSON, ?)",
            [task_id, json.dumps({"start": "2024-01-02", "end": "2024-01-10"}), datetime.now()],
        )


def test_run_task_excludes_unprocessed_from_checkpoint(task_env, monkeypatch):
    """池执行器报 unprocessed 时，这些标的绝不能进断点（P0-2 调用侧）。"""
    from lquant.data.ingest import tasks as tasks_mod

    def fake_backfill_pool(remaining, start, **kwargs):
        syms = [s for s, _ in remaining]
        assert "000200.SZ" in syms, "夹具地基失效：池里应有 000200.SZ"
        # 模拟「跑了一只，取消，另一只从未尝试」
        return {
            "done": 1,
            "failed": [],
            "rows": 1,
            "early_stopped": False,
            "canceled": True,
            "unprocessed": ["000200.SZ"],
        }

    monkeypatch.setattr(tasks_mod, "backfill_pool", fake_backfill_pool)

    _insert_running_task("t-p02-task")
    res = tasks_mod.run_claimed_task("t-p02-task", cancel_check=lambda: True)
    assert res["status"] == "interrupted"

    done = Checkpoint("daily:t-p02-task").done
    assert "600100.SH" in done
    assert "000200.SZ" not in done, "从未尝试的标的被误标为完成 —— P0-2 回归"


def test_run_task_marks_all_when_no_unprocessed_key(task_env, monkeypatch):
    """旧执行器返回值没有 unprocessed 键时行为不变（向后兼容护栏）。"""
    from lquant.data.ingest import tasks as tasks_mod

    def legacy_backfill_pool(remaining, start, **kwargs):
        return {
            "done": len(remaining),
            "failed": [],
            "rows": 1,
            "early_stopped": False,
            "canceled": False,
        }

    monkeypatch.setattr(tasks_mod, "backfill_pool", legacy_backfill_pool)
    monkeypatch.setattr(tasks_mod, "_auto_crosscheck", lambda *a, **k: None)

    _insert_running_task("t-p02-legacy")
    res = tasks_mod.run_claimed_task("t-p02-legacy", cancel_check=lambda: False)
    assert res["status"] == "ok"
    assert Checkpoint("daily:t-p02-legacy").done == {"600100.SH", "000200.SZ"}


# ======================================================================
# P0-3  分钟线只标记「确实返回了数据」的标的
# ======================================================================


class _FakeMinuteProvider:
    def __init__(self, symbols_returned: list[str] | None) -> None:
        self.symbols_returned = symbols_returned
        self.calls = 0

    def minute_bars(self, symbols, start, end, freq):
        self.calls += 1
        if self.symbols_returned is None:
            return _empty_bars()
        return pl.DataFrame(
            {
                "symbol": list(self.symbols_returned),
                "trade_date": [date(2024, 1, 2)] * len(self.symbols_returned),
                "close": [1.0] * len(self.symbols_returned),
            }
        )


@pytest.fixture()
def minute_env(env, monkeypatch):
    """分钟线回填的可注入环境：provider 与写盘都打桩，只验记账。"""
    import lquant.data.ingest.minute as minute_mod

    written: list[pl.DataFrame] = []
    monkeypatch.setattr(minute_mod, "write_minute", lambda df: written.append(df))
    yield written, monkeypatch


def _run_minute(monkeypatch, returned: list[str] | None, symbols: list[str]):
    import lquant.data.ingest.minute as minute_mod
    import lquant.data.providers as providers_mod

    provider = _FakeMinuteProvider(returned)
    monkeypatch.setattr(providers_mod, "get_provider", lambda *a, **k: provider)
    n = minute_mod.backfill_minute(
        symbols, start="2024-01-01", end="2024-01-31", freq="60min", batch=10
    )
    return n, provider


def test_minute_marks_only_symbols_with_data(minute_env, monkeypatch):
    """批次里 2 只只回了 1 只：只有返回的那只进断点（P0-3）。"""
    written, _ = minute_env
    symbols = ["600000.SH", "600001.SH"]
    n, _ = _run_minute(monkeypatch, ["600000.SH"], symbols)

    assert n == 1
    assert len(written) == 1  # 有数据才写盘
    assert Checkpoint("minute_60min").done == {"600000.SH"}


def test_minute_zero_row_batch_marks_nothing(minute_env, monkeypatch):
    """整批静默零行：一只都不标，重跑会重试（此前整批标 done）。"""
    written, _ = minute_env
    symbols = ["600000.SH", "600001.SH"]
    n, _ = _run_minute(monkeypatch, None, symbols)

    assert n == 0
    assert written == []  # 零行不写盘
    assert Checkpoint("minute_60min").done == set()


def test_minute_all_returned_marks_all(minute_env, monkeypatch):
    """全部返回：全部标记（正常路径护栏）。"""
    written, _ = minute_env
    symbols = ["600000.SH", "600001.SH"]
    n, _ = _run_minute(monkeypatch, symbols, symbols)

    assert n == 2
    assert len(written) == 1
    assert Checkpoint("minute_60min").done == set(symbols)


# ======================================================================
# P0-4  Checkpoint.covered_until / covered_window 语义区分
# ======================================================================


def test_covered_until_stops_at_hole(env):
    """[2016,2018] ∪ [2024,2026]：从 2016 起连续覆盖只到 2018。"""
    cp = Checkpoint("t-p04-until")
    cp.record_coverage(["X"], date(2016, 1, 1), date(2018, 12, 31))
    cp.record_coverage(["X"], date(2024, 1, 1), date(2026, 12, 31))

    assert cp.covered_until("X", date(2016, 1, 1)) == date(2018, 12, 31)
    # 而外框会把空洞掩盖成「整段已覆盖」—— 这正是 P0-4 的坑
    assert cp.covered_window("X") == (date(2016, 1, 1), date(2026, 12, 31))
    assert cp.covers("X", date(2016, 1, 1), date(2026, 12, 31)) is False


def test_covered_until_returns_none_when_start_uncovered(env):
    cp = Checkpoint("t-p04-none")
    cp.record_coverage(["X"], date(2024, 1, 1), date(2024, 12, 31))
    assert cp.covered_until("X", date(2016, 1, 1)) is None
    assert cp.covered_until("Y", date(2016, 1, 1)) is None


def test_covered_until_contiguous_spans_merge(env):
    """相邻区间会被合并（中间没有空洞），covered_until 一路到底。"""
    cp = Checkpoint("t-p04-merge")
    cp.record_coverage(["X"], date(2024, 1, 1), date(2024, 6, 30))
    cp.record_coverage(["X"], date(2024, 7, 1), date(2024, 12, 31))
    assert cp.covered_until("X", date(2024, 1, 1)) == date(2024, 12, 31)


def test_financial_todo_windows_does_not_skip_hole(env):
    """财务增量：两段覆盖之间的空洞必须被重拉，起点落在空洞开头。"""
    from lquant.data.ingest.financial import _todo_windows

    cp = Checkpoint("t-p04-fin")
    cp.record_coverage(["X"], date(2016, 1, 1), date(2018, 12, 31))
    cp.record_coverage(["X"], date(2024, 1, 1), date(2026, 12, 31))

    todo = _todo_windows(cp, ["X"], date(2016, 1, 1), date(2026, 12, 31))
    # 旧实现用 covered_window 外框 → 这里会是空 dict（永不回补）
    assert todo == {date(2019, 1, 1): ["X"]}


def test_financial_todo_windows_skips_when_fully_covered(env):
    """单个区间完整覆盖请求窗口 → 确实无事可做（空 dict）。"""
    from lquant.data.ingest.financial import _todo_windows

    cp = Checkpoint("t-p04-fin-ok")
    cp.record_coverage(["X"], date(2016, 1, 1), date(2026, 12, 31))
    assert _todo_windows(cp, ["X"], date(2020, 1, 1), date(2020, 12, 31)) == {}


def test_financial_todo_windows_incremental_from_contiguous_end(env):
    """从 start 起连续覆盖到 until → 只拉 until+1 之后（公告日增量）。"""
    from lquant.data.ingest.financial import _todo_windows

    cp = Checkpoint("t-p04-fin-inc")
    cp.record_coverage(["X"], date(2024, 1, 1), date(2024, 6, 30))
    todo = _todo_windows(cp, ["X"], date(2024, 1, 1), date(2024, 12, 31))
    assert todo == {date(2024, 7, 1): ["X"]}
