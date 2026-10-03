"""算子目录自省 —— 前端因子编辑画布据此动态生成积木。

`Registry.describe()` 只有 category / min_window / label，**没有元数**。
元数（吃几个序列输入）与标量参数只能从算子函数签名推导，这里集中推导一次，
API 与测试共用。

约定（与 `ops/ts_ops.py` 等既有算子一致）：
    - 注解含 `Expr` 的参数 → 序列输入端口
    - 注解为 `int` / `float` / `bool` → 标量参数
    - 名为 `n` 的标量参数 → 窗口长度（前端渲染成窗口控件）
    - 其余标量参数 → 普通数值（如 Ts_Quantile 的 q、Power 的 p）

前端**不得**自持算子定义 —— 语义分歧（Greater 取大 vs 比较、Ts_ArgMax 的
0/1 基准、Log 的 log1p 口径）全部以这里返回的 label 为准，画布照抄。
"""
from __future__ import annotations

import inspect
from typing import Any

from lquant.factors.ops.registry import OPS

#: 视为标量的注解文本（`from __future__ import annotations` 下注解是字符串）
_SCALAR_ANNOTATIONS: frozenset[str] = frozenset({"int", "float", "bool"})

#: 窗口参数的习惯命名 —— 与 DSL 位置参数顺序一致（Ts_Mean(x, n)）
_WINDOW_PARAM = "n"

#: DSL 语法内建的中缀算子 —— 不在 OPS 注册表，由 `dsl/parser.py` 直接处理。
#: 画布要搭出四则与比较就需要它们，仍由服务端声明、前端不猜：`<`/`>` 只出
#: 0/1（供 CNTP 族与 If），且没有 `<=`/`>=`/`==`/`%` 这些容易想当然的算子。
INFIX_OPERATORS: tuple[dict[str, Any], ...] = (
    {"token": "+", "label": "加法", "arity": 2},
    {"token": "-", "label": "减法", "arity": 2},
    {"token": "*", "label": "乘法", "arity": 2},
    {"token": "/", "label": "除法", "arity": 2},
    {"token": ">", "label": "大于（输出 0/1）", "arity": 2},
    {"token": "<", "label": "小于（输出 0/1）", "arity": 2},
    # 一元负号与二元减法共用 token，靠 arity 区分。画布必须支持它，
    # 否则打开 `-$close` 这类已有因子时只能改写成 `(0-$close)` ——
    # canonical 不折叠这种改写，往返就不再稳定（有损）。
    {"token": "-", "label": "取相反数", "arity": 1},
)


def infix_catalog() -> list[dict[str, Any]]:
    """语法级中缀算子清单（画布用它们搭四则与比较）。"""
    return [dict(item) for item in INFIX_OPERATORS]


def _annotation_text(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        return annotation.strip()
    return getattr(annotation, "__name__", str(annotation)).strip()


def _param_kind(annotation: Any) -> str:
    """返回 'series' 或 'scalar'。

    未知注解按序列处理：多一个输入端口会让前端画布明显搭不出表达式，
    而漏一个端口会静默生成元数不对的调用 —— 前者可发现，后者不可。
    """
    text = _annotation_text(annotation)
    if text in _SCALAR_ANNOTATIONS:
        return "scalar"
    return "series"


#: 签名不可自省时的哨兵元数。**不能返回 0** —— 那会让画布把 `Name()` 当成
#: 零参调用发出去，而服务端静态检查不校验元数，坏表达式会一路走到求值才炸。
UNKNOWN_ARITY = -1


def _introspect(fn: Any) -> tuple[int, list[dict[str, Any]]]:
    """返回 (序列输入个数, 标量参数描述列表)。

    拿不到签名（C 扩展 / pyfunction）时返回 UNKNOWN_ARITY 哨兵，
    前端据此禁用该积木，而不是静默当成零参算子。
    """
    series_arity = 0
    params: list[dict[str, Any]] = []
    seen_scalar = False
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # 内建/扩展函数拿不到签名
        return UNKNOWN_ARITY, []

    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if _param_kind(param.annotation) == "series":
            if seen_scalar:
                # 画布按「序列在前、标量在后」拼位置实参；签名一旦不是这个
                # 形状就没法安全生成调用，宁可标成不可用。
                return UNKNOWN_ARITY, []
            series_arity += 1
            continue
        seen_scalar = True
        has_default = param.default is not inspect.Parameter.empty
        default: float | None = None
        if has_default:
            try:
                default = float(param.default)
            except (TypeError, ValueError):
                # 非数值默认值不该让整个目录接口 500
                default = None
                has_default = False
        params.append({
            "name": param.name,
            "type": "window" if param.name == _WINDOW_PARAM else "number",
            "required": not has_default,
            "default": default,
        })
    return series_arity, params


def op_catalog() -> list[dict[str, Any]]:
    """全部已注册算子的目录（按名称排序，与 `OPS.keys()` 同序）。"""
    catalog: list[dict[str, Any]] = []
    for name in OPS:  # Registry.__iter__ 按名称排序（与 keys() 同序）
        meta = OPS.meta(name)
        series_arity, params = _introspect(OPS.get(name))
        catalog.append({
            "name": name,
            "category": meta.get("category", "EL"),
            "label": meta.get("label", name),
            "min_window": int(meta.get("min_window", 0)),
            "series_arity": series_arity,
            "params": params,
        })
    return catalog
