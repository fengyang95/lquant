"""analysis 沙箱：执行 + 输出 schema 校验。"""
import polars as pl
import pytest

from lquant.backtest.analysis import AnalysisError, run_user_analysis

PAYLOAD = {
    "dates": ["2026-01-05", "2026-01-06"],
    "nav": [1.0, 1.01],
    "returns": [0.0, 0.01],
    "trades": pl.DataFrame({"trade_date": [], "symbol": [], "qty": []}),
    "positions": {}, "records": {}, "metrics": {"sharpe": 1.2},
}


def test_chart_output():
    src = """
def analyze(result):
    import statistics
    return [{"type": "chart", "title": "t", "data": [{"x": d, "ret": r}
             for d, r in zip(result["dates"], result["returns"])],
             "x": "x", "ys": ["ret"]}]
"""
    out = run_user_analysis(src, PAYLOAD)
    assert out[0]["type"] == "chart" and out[0]["ys"] == ["ret"]


def test_table_output():
    src = """
def analyze(result):
    return [{"type": "table", "title": "分年", "columns": ["year", "ret"],
             "rows": [["2026", 0.1]]}]
"""
    out = run_user_analysis(src, PAYLOAD)
    assert out[0]["type"] == "table"


def test_bad_import_rejected():
    with pytest.raises(AnalysisError):
        run_user_analysis("import os\ndef analyze(r): return []", PAYLOAD)


def test_bad_schema_rejected():
    with pytest.raises(AnalysisError):
        run_user_analysis("def analyze(r): return [{'type': 'pie'}]", PAYLOAD)


def test_runtime_error_wrapped():
    # 原brief match="1, 0"：3.14 上 ZeroDivisionError str 是 "division by zero"，
    # 无 "1, 0" 字样，无法匹配——保留源码 `1/0 and []`，改匹配 "zero"。
    with pytest.raises(AnalysisError, match="zero"):
        run_user_analysis("def analyze(r): return 1/0 and []", PAYLOAD)
