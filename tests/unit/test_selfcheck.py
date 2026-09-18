"""backtest/selfcheck 单元测试：run_selfcheck 全项通过 + 异常兜底路径。"""

from __future__ import annotations

import lquant.backtest.selfcheck as sc
from lquant.backtest.selfcheck import STAMP_CUT, run_selfcheck


def test_run_selfcheck_all_pass():
    checks = run_selfcheck()
    names = [c["name"] for c in checks]
    assert len(checks) == 8
    assert any("金标准净值" in n for n in names)
    assert any("现金守恒" in n for n in names)
    assert any("防未来函数" in n for n in names)
    assert any("涨跌停拒单" in n for n in names)
    assert any("T+N" in n for n in names)
    assert any("印花税" in n for n in names)
    assert any("最低佣金" in n for n in names)
    assert any("绩效指标" in n for n in names)
    failed = [c for c in checks if not c["passed"]]
    assert failed == [], f"自检失败项: {failed}"


def test_run_selfcheck_captures_exception_per_item(monkeypatch):
    """单项炸掉不拖垮其它项，detail 记录异常类型。"""

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("引擎炸了")

    monkeypatch.setattr(sc, "Engine", _Boom)
    checks = run_selfcheck()
    assert len(checks) == 8
    engine_based = [c for c in checks if not c["passed"]]
    assert engine_based, "至少 Engine 相关项应失败"
    for c in engine_based:
        assert c["detail"].startswith("异常: RuntimeError")
        assert c["passed"] is False
    # 非 Engine 项（印花税/最低佣金/绩效指标）仍通过
    assert any(c["passed"] and "印花税" in c["name"] for c in checks)
    assert any(c["passed"] and "绩效指标" in c["name"] for c in checks)


def test_stamp_cut_date():
    assert STAMP_CUT.year == 2023 and STAMP_CUT.month == 8 and STAMP_CUT.day == 28
