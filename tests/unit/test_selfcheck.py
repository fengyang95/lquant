"""backtest/selfcheck 单元测试：run_selfcheck 全项通过 + 异常兜底路径。"""

from __future__ import annotations

import lquant.backtest.selfcheck as sc
from lquant.backtest.selfcheck import STAMP_CUT, run_selfcheck


def test_run_selfcheck_all_pass():
    """自检必须全绿，且覆盖 L1~L4 各层的关键项。

    只断言「必需项都在且都通过」，**不锁定条数** —— 锁定条数会让每次
    新增自检项都要改测试，且失败信息毫无信息量。
    """
    checks = run_selfcheck()
    names = [c["name"] for c in checks]
    required = ["金标准净值", "现金守恒", "防未来函数", "涨跌停拒单", "T+N",
                "印花税", "最低佣金", "绩效指标",
                # 2026-10-02 复审新增
                "tick 取整", "除权日新建仓不欠配", "印花税历史区间"]
    for key in required:
        assert any(key in n for n in names), f"缺少自检项 {key!r}：{names}"
    failed = [c for c in checks if not c["passed"]]
    assert failed == [], f"自检失败项: {failed}"


def test_run_selfcheck_captures_exception_per_item(monkeypatch):
    """单项炸掉不拖垮其它项，detail 记录异常类型。"""

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("引擎炸了")

    monkeypatch.setattr(sc, "Engine", _Boom)
    checks = run_selfcheck()
    engine_based = [c for c in checks if not c["passed"]]
    assert engine_based, "至少 Engine 相关项应失败"
    for c in engine_based:
        assert c["detail"].startswith("异常: RuntimeError")
        assert c["passed"] is False
    # 不依赖 Engine 的项（印花税/最低佣金/绩效指标）仍通过
    assert any(c["passed"] and "印花税" in c["name"] for c in checks)
    assert any(c["passed"] and "绩效指标" in c["name"] for c in checks)


def test_stamp_cut_date():
    assert STAMP_CUT.year == 2023 and STAMP_CUT.month == 8 and STAMP_CUT.day == 28
