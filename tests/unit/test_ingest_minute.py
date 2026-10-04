"""minute 回填的 checkpoint 语义测试：失败批次不得标 done（否则永久缺口）。

注意：**空帧现在是「源静默零行」= 未取到**（P0-3），不能再当作「成功但
不写湖」的替身 —— 成功批必须真的返回行，写盘另行打桩。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.ingest.checkpoint import Checkpoint


class _FlakyMinuteProvider:
    name = "baostock"

    def __init__(self, fail_symbols: set[str]) -> None:
        self._fail = fail_symbols
        self.calls: list[list[str]] = []

    def minute_bars(self, chunk, start_d, end_d, freq):
        self.calls.append(list(chunk))
        if self._fail & set(chunk):
            raise RuntimeError("network glitch")
        # 成功批必须**真的返回行**：空帧现在语义是「源静默零行」= 未取到
        # （P0-3），不能再拿它当「成功但不写湖」的替身。
        return pl.DataFrame({
            "symbol": list(chunk),
            "trade_date": [date(2026, 8, 3)] * len(chunk),
            "close": [1.0] * len(chunk),
        })


def _run(tmp_path, monkeypatch, fail_symbols):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:

        fake = _FlakyMinuteProvider(fail_symbols)
        monkeypatch.setattr("lquant.data.providers.get_provider",
                            lambda *a, **k: fake)
        from lquant.data.ingest import minute as mod

        # 写盘打桩：本用例只验 checkpoint 记账，不碰湖
        monkeypatch.setattr(mod, "write_minute", lambda df: None)
        out = mod.backfill_minute(
            ["A", "B", "C", "D"], start="2026-08-03", end="2026-08-07", batch=2)
        assert out == 2  # 只有成功批计入
        cp = Checkpoint("minute_60min")
        assert cp.remaining(["A", "B", "C", "D"]) == ["C", "D"]  # 失败批未标
        return fake
    finally:
        get_settings.cache_clear()


def test_minute_failed_batch_not_marked(tmp_path, monkeypatch) -> None:
    """拉取异常的批次不得进 checkpoint，重跑自动重试。"""
    fake = _run(tmp_path, monkeypatch, {"C"})
    assert fake.calls == [["A", "B"], ["C", "D"]]


def test_minute_retry_after_failure(tmp_path, monkeypatch) -> None:
    """失败批重跑时被重试；成功后 checkpoint 收口。"""
    fake = _run(tmp_path, monkeypatch, {"C"})
    assert fake.calls == [["A", "B"], ["C", "D"]]
    # 第二轮：provider 恢复，重跑只剩失败批
    fake._fail = set()
    from lquant.data.ingest import minute as mod

    out = mod.backfill_minute(
        ["A", "B", "C", "D"], start="2026-08-03", end="2026-08-07", batch=2)
    assert out == 2  # 只拉 C、D
    cp = Checkpoint("minute_60min")
    assert cp.remaining(["A", "B", "C", "D"]) == []
