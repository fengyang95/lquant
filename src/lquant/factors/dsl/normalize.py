"""表达式归一化：历史遗留的 qlib 写法 → lquant DSL。

早期 qlib 种子把**原始 qlib 公式**直接写进了 ``factor_def.expression``
（``Slope($close,10)/$close``、``Mean($close,20)/$close``…）。而画布反解析、
静态校验、因子计算只认 lquant DSL —— 打开这类因子会 422（未注册算子 Slope），
现算会 500。它们是完全合法的历史数据，不该逼用户先手工重灌种子。

统一在这里过一道兼容翻译（复用 ``sources/qlib_source.translate``，即统一引擎
的翻译器，不另起一套口径），调用方仍只面对一种语法：

- 已经是合法 lquant DSL → 原样返回；
- 否则尝试按 qlib 公式翻译，翻译后仍需通过静态检查才认；
- 两者都失败 → 抛**原始**的 DSL 报错。qlib 翻译失败只说明「它不是 qlib 写法」，
  不该拿它覆盖更准确的 DSL 报错（``Nope($close)`` 要报「未注册算子: Nope」，
  而不是「不可翻译的 qlib 算子: Nope」）。

**为什么要顺带校验参数个数**：lquant 的 ``Rank(x)`` 是截面秩（1 参），
qlib 的 ``Rank(x,n)`` 是时序秩（2 参）—— 同名不同义。静态检查只认算子名、
不认元数，于是 ``Rank($close,5)`` 会被当成 lquant 截面秩直接放行，求值时
才在 Polars 里炸出 ``rank() takes 1 positional argument but 2 were given``。
按目录自省的元数区间判一次，元数对不上就交给 qlib 翻译，同名歧义自然消解。

**这里不做字段白名单校验**：引擎会在 ``check(allowed_fields=df.columns)``
里用**真实面板列**校验（合成 / 预处理路径会引用 ``f``、``cov_*`` 这类派生列，
拿固定的日线字段表去卡会误伤）。字段问题仍由引擎在正确的上下文里报。
"""
from __future__ import annotations

from functools import lru_cache

from lquant.core.errors import FactorError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp
from lquant.factors.dsl.parser import parse

#: qlib 专有字段：lquant 面板没有对应列（qlib 的 ``$vwap`` 用 amount/volume 代理）。
#: 含它的表达式不可能是合法 lquant DSL，直接走 qlib 翻译（顺带剥掉 ``#`` 注释）。
_QLIB_ONLY_FIELDS: tuple[str, ...] = ("$vwap",)


@lru_cache(maxsize=1)
def _arity_bounds() -> dict[str, tuple[int, int]]:
    """算子名 → (最少实参个数, 最多实参个数)，取自服务端目录自省。

    自省不出签名（``series_arity < 0``）的算子不进表 —— 元数未知时宁可
    不判，也不能凭猜测拒绝一个可能合法的调用。
    """
    from lquant.factors.ops.catalog import op_catalog

    bounds: dict[str, tuple[int, int]] = {}
    for op in op_catalog():
        series = op["series_arity"]
        if series < 0:
            continue
        required = sum(1 for p in op["params"] if p.get("required"))
        bounds[op["name"]] = (series + required, series + len(op["params"]))
    return bounds


def _check_arity(node: Node) -> None:
    """递归校验每个 Call 的实参个数落在目录声明的区间内。"""
    if isinstance(node, (Field, Num)):
        return
    if isinstance(node, UnaryOp):
        _check_arity(node.arg)
        return
    if isinstance(node, BinaryOp):
        _check_arity(node.left)
        _check_arity(node.right)
        return
    if isinstance(node, Call):
        bound = _arity_bounds().get(node.name)
        if bound is not None:
            low, high = bound
            if not low <= len(node.args) <= high:
                expect = f"{low}" if low == high else f"{low}~{high}"
                raise FactorError(
                    f"算子 {node.name} 参数个数不对：期望 {expect} 个，实际 {len(node.args)} 个")
        for arg in node.args:
            _check_arity(arg)
        return
    raise TypeError(f"未知节点 {type(node)}")


def _lquant_ok(expression: str) -> None:
    """校验「按 lquant DSL 解释」是否成立：语法 + 算子 + 元数（字段交给引擎）。"""
    ast = parse(expression, "normalize")
    check(ast)
    _check_arity(ast.root)


def _translate(expression: str) -> str:
    """qlib 翻译 + 翻译结果再校验（翻译本身不许产出坏 DSL）。"""
    from lquant.factors.sources.qlib_source import translate

    translated = translate(expression)
    _lquant_ok(translated)
    return translated


def normalize(expression: str) -> str:
    """把 ``expression`` 归一成 lquant DSL（空串原样返回）。"""
    expr = (expression or "").strip()
    if not expr:
        return expr
    # qlib 专有字段：lquant 解释必然不成立（面板里没有这一列），直接翻译
    if any(field in expr for field in _QLIB_ONLY_FIELDS):
        return _translate(expr)
    try:
        _lquant_ok(expr)
        return expr
    except Exception as dsl_error:  # noqa: BLE001  不是合法 DSL，试 qlib 兼容翻译
        try:
            return _translate(expr)
        except Exception:  # noqa: BLE001  也不是 qlib 写法 → 保留原始 DSL 报错
            raise dsl_error from None


def normalize_soft(expression: str) -> str:
    """``normalize`` 的 fail-soft 版本：翻译不了就原样返回。

    只给「读列表 / 详情」这类**展示**路径用 —— 一条坏表达式不该让整页 500；
    真正要执行 / 落库的路径必须用 ``normalize`` 如实报错。
    """
    try:
        return normalize(expression)
    except Exception:  # noqa: BLE001
        return expression or ""
