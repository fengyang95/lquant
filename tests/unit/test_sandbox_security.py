"""用户代码沙箱收口：静态黑名单 + 执行侧受限内建。

对应 docs/SECURITY.md 的第 1、2 层。第 3 层（默认只绑回环）在 lquant.sh 里，
不由本文件覆盖。
"""
from __future__ import annotations

import builtins

import pytest

from lquant.backtest import sandbox
from lquant.backtest.analysis import AnalysisError, run_user_analysis
from lquant.backtest.validation import validate_source

PAYLOAD: dict = {
    "dates": ["2026-01-05", "2026-01-06"],
    "nav": [1.0, 1.01],
    "returns": [0.0, 0.01],
    "trades": None,
    "positions": {}, "records": {}, "metrics": {"sharpe": 1.2},
}


def _analysis_src(body: str) -> str:
    return f"def analyze(result):\n    {body}\n    return []"


# ---------- 第 1 层：静态黑名单 ----------


@pytest.mark.parametrize("snippet", [
    '__import__("os")',
    'eval("1+1")',
    'exec("x = 1")',
    'compile("1", "<s>", "eval")',
    'open("/etc/passwd")',
    'getattr(object, "x")',
    'globals()',
    'locals()',
    'setattr(object, "x", 1)',
])
def test_dynamic_execution_calls_rejected(snippet: str) -> None:
    assert validate_source(_analysis_src(f"x = {snippet}"),
                           require_initialize=False), f"应拦截: {snippet}"


@pytest.mark.parametrize("snippet", [
    "().__class__.__bases__[0]",
    "(1).__class__",
    "x.__globals__",
    "x.__subclasses__()",
    "x.__dict__",
    "x.__code__",
])
def test_object_graph_escape_rejected(snippet: str) -> None:
    assert validate_source(_analysis_src(f"x = {snippet}"),
                           require_initialize=False), f"应拦截: {snippet}"


def test_bare_dunder_builtins_rejected() -> None:
    assert validate_source(_analysis_src("return __builtins__"),
                           require_initialize=False)


def test_normal_analysis_and_strategy_still_pass() -> None:
    """黑名单不能把正常写法误伤 —— 这是最容易翻车的地方。"""
    ok_analysis = """
def analyze(result):
    import statistics
    vals = [float(x) for x in result["returns"]]
    mean = statistics.mean(vals) if vals else 0.0
    return [{"type": "table", "columns": ["n", "mean"], "rows": [[len(vals), mean]]}]
"""
    assert validate_source(ok_analysis, require_initialize=False) == []

    ok_strategy = """
def initialize(context):
    set_benchmark('000300.SH')

def handle_data(context):
    for s in ['600519.SH', '000001.SZ']:
        order_target_value(s, context.portfolio.total_value / 2)
"""
    assert validate_source(ok_strategy) == []


def test_try_except_with_exception_names_still_passes() -> None:
    """受限内建必须包含常见异常类型，否则 except 子句直接 NameError。"""
    src = """
def initialize(context):
    try:
        x = 1 / 0
    except ZeroDivisionError:
        pass
    except (ValueError, TypeError) as e:
        pass
"""
    assert validate_source(src) == []


# ---------- 第 2 层：执行侧受限内建 ----------


def test_safe_builtins_excludes_escape_hatches() -> None:
    """真实内建一个都不能留；__import__ 必须是白名单守卫版。"""
    sb = sandbox.safe_builtins()
    for name in ("eval", "exec", "compile", "open",
                 "getattr", "setattr", "globals", "locals", "input"):
        assert name not in sb, f"{name} 不该出现在受限内建里"
    assert sb["__import__"] is sandbox._guarded_import


def test_guarded_import_allows_whitelist_only() -> None:
    guarded = sandbox.safe_builtins()["__import__"]
    assert guarded("statistics") is not None           # 白名单内放行
    with pytest.raises(ImportError, match="os"):
        guarded("os")                                  # 白名单外拒
    with pytest.raises(ImportError, match="subprocess"):
        guarded("subprocess")


def test_import_statement_inside_user_code_still_works() -> None:
    """import 语句在运行期走 __import__ —— 守卫版必须放行白名单内模块，
    否则正常的 `import statistics` 会被误杀。"""
    src = """
def analyze(result):
    import statistics
    return [{"type": "table", "columns": ["n"],
             "rows": [[len([x for x in result["returns"]])]]}]
"""
    out = run_user_analysis(src, PAYLOAD)
    assert out[0]["type"] == "table"


def test_import_statement_outside_whitelist_blocked_at_runtime() -> None:
    """静态层会拦 `import os`；这里绕过静态层直接 exec，验证运行期守卫也拦。"""
    from lquant.backtest.sandbox import safe_builtins

    ns: dict = {"__builtins__": safe_builtins()}
    with pytest.raises(ImportError, match="os"):
        exec("import os", ns)


def test_every_safe_builtin_name_exists_in_builtins() -> None:
    """防止白名单里写错名字（getattr 会直接抛，早失败）。"""
    missing = sorted(n for n in sandbox.SAFE_BUILTIN_NAMES
                     if not hasattr(builtins, n))
    assert missing == []


def test_safe_builtins_returns_fresh_copy_each_call() -> None:
    a, b = sandbox.safe_builtins(), sandbox.safe_builtins()
    assert a is not b
    a["len"] = None  # 改动不应污染下一份
    assert b["len"] is builtins.len


def test_analysis_namespace_overrides_builtins() -> None:
    """命名空间必须显式设 __builtins__，否则 CPython 注入完整内建。"""
    from lquant.backtest.analysis import _ns

    ns = _ns()
    assert "__builtins__" in ns
    assert ns["__builtins__"]["__import__"] is sandbox._guarded_import
    assert "eval" not in ns["__builtins__"]


def test_runtime_layer_blocks_non_call_builtin_reference() -> None:
    """`globals` 作为「值」引用时不命中静态黑名单（只拦调用），
    但执行侧受限内建应让它 NameError —— 证明第 2 层真的在起作用。"""
    src = _analysis_src("f = globals")
    assert validate_source(src, require_initialize=False) == []   # 静态放行
    with pytest.raises(AnalysisError, match="globals"):
        run_user_analysis(src, PAYLOAD)


def test_jq_runner_cannot_reach_dunder_import() -> None:
    """JQRunner 被直接构造时（绕过 API 静态闸）仍挡住 __import__。"""
    from lquant.backtest.jqapi import JQRunner

    with pytest.raises(ValueError, match="策略代码执行失败"):
        JQRunner("x = __import__('os').getcwd()")
