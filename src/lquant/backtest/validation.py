"""策略/分析源码静态校验：语法 + import 白名单 + 危险调用黑名单 + 入口函数。

注意：静态校验只是**第一道闸**，挡的是「一句话 RCE」这类老实写法。
真正的收口在执行侧（``lquant.backtest.sandbox`` 的受限内建）与服务默认只绑
``127.0.0.1``。见 ``docs/SECURITY.md``。
"""
from __future__ import annotations

import ast

from lquant.backtest.sandbox import (
    ALLOWED_IMPORTS,
    FORBIDDEN_ATTRS,
    FORBIDDEN_CALL_NAMES,
    FORBIDDEN_GLOBAL_NAMES,
)

__all__ = ["ALLOWED_IMPORTS", "validate_source"]


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
        elif isinstance(node, ast.Call):
            # 只认「名字直接调用」，如 __import__("os") / eval(...) / open(...)；
            # 属性式调用（result.sort()）不在此列。
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
                errs.append(f"行 {node.lineno}: 不允许调用 {func.id}()")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                errs.append(f"行 {node.lineno}: 不允许访问属性 {node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_GLOBAL_NAMES:
            errs.append(f"行 {node.lineno}: 不允许引用 {node.id}")

    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if require_initialize and "initialize" not in names:
        errs.append("必须定义 initialize(context) 入口函数")
    return errs
