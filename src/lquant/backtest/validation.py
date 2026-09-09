"""策略/分析源码静态校验：语法 + import 白名单 + 入口函数。"""
from __future__ import annotations

import ast

ALLOWED_IMPORTS = frozenset({
    "math", "datetime", "collections", "itertools", "functools",
    "statistics", "numpy", "pandas", "polars",
})


def validate_source(source: str, *, require_initialize: bool = True) -> list[str]:
    """返回错误列表（空 = 通过）。错误带行号。"""
    errs: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"语法错误（行 {e.lineno or '?'}）: {e.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    errs.append(f"行 {node.lineno}: 不允许 import {root}（白名单: "
                                f"{', '.join(sorted(ALLOWED_IMPORTS))}）")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                errs.append(f"行 {node.lineno}: 不允许相对导入")
                continue
            root = (node.module or "").split(".")[0]
            if root and root not in ALLOWED_IMPORTS:
                errs.append(f"行 {node.lineno}: 不允许 import {root}")
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if require_initialize and "initialize" not in names:
        errs.append("必须定义 initialize(context) 入口函数")
    return errs
