"""跨源对拍 issue 字段级明细（spec §2.2）。

构造 Issue 时把字段级数值（symbol/field/primary/peer/deviation_pct/level）
塞进 extra —— latest_issues 落库后 detail JSON 会展开这些键，前端明细直接可读。

对拍原则不变：peer 数值只进 issue 明细用于展示，绝不写回湖取值。

隔离方式同 test_crosscheck_settings.py：LQ_ROOT + chdir + cache_clear +
拷贝真实 providers.yaml（_cfg 回退读它）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest


@pytest.fixture
def cc_env(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "config").mkdir()
    import shutil

    shutil.copy2(root / "config" / "providers.yaml",
                 tmp_path / "config" / "providers.yaml")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_issue_extra_has_field_level_values(cc_env, monkeypatch):
    """L2 偏差 issue 的 extra 含 field/primary/peer/deviation_pct/level。"""
    import lquant.data.ingest.crosscheck as cc

    sym = "600000.SH"
    primary = pl.DataFrame({
        "symbol": [sym],
        "trade_date": [date(2024, 1, 2)],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.0],
        "volume": [120_000.0],
    })
    peer = primary.with_columns(pl.col("close") * 1.1)  # 10% 偏差 → L2

    monkeypatch.setattr(cc, "_sample_symbols",
                        lambda limit: ([sym], date(2024, 1, 2), date(2024, 1, 2)))
    monkeypatch.setattr(cc, "_primary_daily",
                        lambda symbols, start, end: primary)
    monkeypatch.setattr(cc, "_peer_daily",
                        lambda name, symbols, start, end: peer)

    out = cc.run_crosscheck(peers=["fake"], start="2024-01-02",
                            end="2024-01-02")
    assert out["issues"], "10% 偏差应落 issue"
    issue = out["issues"][0]
    extra = issue.extra
    assert extra["symbol"] == sym
    assert extra["field"] in ("open", "high", "low", "close", "volume")
    assert extra["field"] == "close"
    assert extra["primary"] == 10.0
    assert extra["peer"] == 11.0
    assert extra["deviation_pct"] == pytest.approx(10.0)
    assert extra["level"] in ("L2", "L3")

    # 落库后 detail JSON 展开同样键（latest_issues 返回 dict）
    from lquant.data.quality.issues import latest_issues

    saved = latest_issues()
    assert saved and set(
        ["field", "primary", "peer", "deviation_pct"]) <= set(saved[0]["detail"])


def test_issue_extra_missing_field_no_fabrication(cc_env, monkeypatch):
    """整行仅 peer 存在（L3）：primary 为 None，deviation_pct 不造数。"""
    import lquant.data.ingest.crosscheck as cc

    sym = "000001.SZ"
    primary = pl.DataFrame({
        "symbol": [sym],
        "trade_date": [date(2024, 1, 2)],
        "close": [None], "volume": [None],
    }, schema_overrides={"close": pl.Float64, "volume": pl.Float64})
    peer = pl.DataFrame({
        "symbol": [sym],
        "trade_date": [date(2024, 1, 2)],
        "close": [10.0],
    })

    monkeypatch.setattr(cc, "_sample_symbols",
                        lambda limit: ([sym], date(2024, 1, 2), date(2024, 1, 2)))
    monkeypatch.setattr(cc, "_primary_daily",
                        lambda symbols, start, end: primary)
    monkeypatch.setattr(cc, "_peer_daily",
                        lambda name, symbols, start, end: peer)

    out = cc.run_crosscheck(peers=["fake"], start="2024-01-02",
                            end="2024-01-02")
    assert out["issues"]
    extra = out["issues"][0].extra
    assert extra["field"] in ("open", "high", "low", "close", "volume")
    assert extra["level"] in ("L2", "L3")
    if extra["field"] == "open":
        assert extra["primary"] is None and extra["peer"] == 9.9
        assert extra["deviation_pct"] is None


def test_peer_window_matches_primary_window(cc_env, monkeypatch):
    """显式 start/end 时 peer 必须用同一窗口（此前 peer 拉全湖 → 假 L3 刷屏）。"""
    import lquant.data.ingest.crosscheck as cc

    sym = "600000.SH"
    primary = pl.DataFrame({
        "symbol": [sym],
        "trade_date": [date(2024, 1, 2)],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [100.0],
    })
    monkeypatch.setattr(cc, "_sample_symbols",
                        lambda limit: ([sym], date(2023, 1, 1), date(2024, 6, 1)))
    monkeypatch.setattr(cc, "_primary_daily",
                        lambda symbols, start, end: primary)
    seen: list[tuple] = []

    def _fake_peer(name, symbols, start, end):
        seen.append((start, end))
        return primary

    monkeypatch.setattr(cc, "_peer_daily", _fake_peer)

    out = cc.run_crosscheck(peers=["fake"], start="2024-01-02",
                            end="2024-01-02")
    assert out["summary"]["checked"] >= 1
    assert seen and seen[0] == (date(2024, 1, 2), date(2024, 1, 2)), \
        f"peer 窗口应为 primary 窗口，实际 {seen[0]}"
