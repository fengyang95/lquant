"""M5/M6 回归：llm-explorer 两段式 + 研报复现归因。"""
from __future__ import annotations

import json


def test_explore_two_stage_seeds_gp():
    from lquant.factors.mining.explore import make_explorer

    f = tmp_jsonl()
    gp = make_explorer(f)
    assert len(gp.pop) >= 1
    expr = gp()
    assert isinstance(expr, str) and expr


def test_load_spec_fail_fast():
    import os
    import tempfile

    from lquant.factors.replication import load_spec
    content = """
name: t_spec
expr: "Rank(Ts_Mean($close,5))"
"""
    fp = tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False)
    fp.write(content)
    fp.close()
    spec = load_spec(fp.name)
    assert spec.name == "t_spec"
    os.unlink(fp.name)


def test_attribute_codes():
    from lquant.factors.replication import attribute

    assert attribute({"ic_mean": -0.05}, {"ic_val": 0.05}, "D") == ["REPORT_SUSPECT"]
    codes = attribute({"ic_mean": 0.05}, {"ic_val": 0.001}, "C", assumptions=["x"])
    assert codes[0] == "PARAM_ASSUMED"
    assert "EXPR_MISREAD" in codes


def tmp_jsonl() -> str:
    import tempfile

    fp = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
    f = fp
    f.write(json.dumps({"expr": "Rank(Ts_Mean($close,5))"}))
    f.close()
    return fp.name
