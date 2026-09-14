"""孤儿校验模块接入 run_lake_checks：adjustment / universe / golden。"""
from __future__ import annotations

from datetime import date

import polars as pl


def _lake_frame(closes: tuple[float, float]) -> pl.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i else close
        rows.append({
            "symbol": "600000.SH",
            "trade_date": date(2026, 8, 3 + i),
            "open": prev, "high": close, "low": prev * 0.99,
            "close": close, "pre_close": prev,
            "volume": 1_000_000.0, "amount": 1.0e8,
            "adj_factor": 1.0,
        })
    return pl.DataFrame(rows)


def _prep(tmp_path, monkeypatch, lake: pl.DataFrame) -> None:
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.data.store.parquet import write_daily

    write_daily(lake)


def test_lake_checks_surface_adj_anomaly(tmp_path, monkeypatch) -> None:
    """close 翻倍而 adj_factor 不变 → run_lake_checks 检出 ADJ_ANOMALY 并落库。"""
    _prep(tmp_path, monkeypatch, _lake_frame((100.0, 200.0)))
    from lquant.data.quality.pipeline import run_lake_checks
    from lquant.data.quality.issues import latest_issues

    found = run_lake_checks()
    assert any(i.rule == "ADJ_ANOMALY" for i in found), \
        f"应检出 ADJ_ANOMALY，实际 rules={[i.rule for i in found]}"
    rows = latest_issues()
    assert any(r["rule_code"] == "ADJ_ANOMALY" for r in rows), \
        f"ADJ_ANOMALY 应落库，实际={[r['rule_code'] for r in rows]}"


def test_lake_checks_clean_lake_no_adj_anomaly(tmp_path, monkeypatch) -> None:
    """正常湖：不误报 ADJ_ANOMALY，且原有 validators 正常返回。"""
    _prep(tmp_path, monkeypatch, _lake_frame((100.0, 101.0)))
    from lquant.data.quality.pipeline import run_lake_checks

    found = run_lake_checks()
    assert isinstance(found, list)
    assert not any(i.rule == "ADJ_ANOMALY" for i in found), \
        f"正常湖不应误报，实际 rules={[i.rule for i in found]}"


def test_lake_checks_golden_degrades_silently(tmp_path, monkeypatch) -> None:
    """golden 无冻结 case（表缺失等）→ 不抛错，检查链路不断。"""
    _prep(tmp_path, monkeypatch, _lake_frame((100.0, 101.0)))
    from lquant.data.quality.pipeline import run_lake_checks

    found = run_lake_checks()  # 不应抛异常
    assert isinstance(found, list)
