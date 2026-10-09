"""静态分析：最小窗口 + 未来函数检测。

黑盒 Python 函数做不到，字符串 DSL 可以逐节点校验 ——
这是防未来函数的第二道防线（第一道是 PIT 数据的 pub_date）。
"""
from __future__ import annotations

from lquant.core.errors import FactorError, LookaheadError
from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp
from lquant.factors.ops.registry import OPS

# 允许读取「当前及过去」的算子；若出现这些前缀之外的未来语义算子则报错
FUTURE_MARKERS = {"future", "lead", "next", "forward"}


def _num_value(a: Node) -> float | None:
    """提取常量参数的数值：Num 直取；UnaryOp(-Num)（如 -1）取负值。

    负窗口（Ts_Delay($close, -1)）经 parser 解析成 UnaryOp，此前既不参与
    窗口估计、也没有任何数值校验 —— 编译成 shift(-1) 直接引用未来数据。
    """
    if isinstance(a, Num):
        return float(a.value)
    if isinstance(a, UnaryOp) and getattr(a, "op", "") in ("-", "USub", "neg") \
            and isinstance(a.arg, Num):
        return -float(a.arg.value)
    return None


def analyze(node: Node) -> tuple[int, set[str], set[str]]:
    """返回 (min_window, fields, op_names)。"""
    if isinstance(node, Field):
        return 0, {node.name}, set()
    if isinstance(node, Num):
        return 0, set(), set()
    if isinstance(node, UnaryOp):
        return analyze(node.arg)
    if isinstance(node, BinaryOp):
        lw, lf, lo = analyze(node.left)
        rw, rf, ro = analyze(node.right)
        return max(lw, rw), lf | rf, lo | ro
    if isinstance(node, Call):
        if node.name.lower().split("_")[0] in FUTURE_MARKERS:
            raise LookaheadError(f"算子 {node.name} 引用未来数据")
        if node.name not in OPS:
            from lquant.core.errors import FactorError

            raise FactorError(f"未注册算子: {node.name}，可选: {OPS.keys()[:20]}")
        w, fields, ops = 0, set(), {node.name}
        for a in node.args:
            aw, af, ao = analyze(a)
            w = max(w, aw)
            fields |= af
            ops |= ao
        meta = OPS.meta(node.name)
        required = int(meta.get("min_window", 0))
        # 负常量拦截仅对 **TS 类**算子生效：TS 算子里负数值参数只可能是
        # 负窗口（shift(-n) 引用未来数据）；正的小数参数是合法的
        # （如 Ts_Quantile 的 q=0.8）。EL 类数值参数是数学常量
        # （如 Power(x, -2)），负值合法。位移类窗口 ≥1 由算子构造期
        # _require_positive 精确拦截。
        is_ts = meta.get("category") == "TS"
        for a in node.args:
            v = _num_value(a)
            if v is None:
                continue
            if is_ts and v < 0:
                raise LookaheadError(
                    f"时序算子 {node.name} 的参数 {v:g} 为负 —— 负窗口引用未来数据")
            required = max(required, int(v))
        return max(w, required), fields, ops
    raise TypeError(f"未知节点 {type(node)}")


def check(expr_ast: object, allowed_fields: set[str] | None = None) -> None:
    """入口：对 FactorExpr 做静态检查，失败直接抛。

    allowed_fields 非 None 时额外做字段白名单校验 —— 拼错字段（$closs）
    在静态期报错并给 difflib 近似候选，而不是算出全 null 静默污染 IC（缺陷 #6）。
    """
    from lquant.factors.dsl.ast_nodes import FactorExpr

    if not isinstance(expr_ast, FactorExpr):
        raise TypeError("需要 FactorExpr")
    w, fields, _ops = analyze(expr_ast.root)
    if allowed_fields is not None:
        import difflib

        for f in sorted(fields - allowed_fields):
            cand = difflib.get_close_matches(f, sorted(allowed_fields), n=3)
            hint = f"，是否想用: {cand}?" if cand else ""
            raise FactorError(
                f"字段 {f!r} 不在数据列中{hint}；可用字段: {sorted(allowed_fields)}")
    expr_ast.min_window = w
    expr_ast.fields = fields
