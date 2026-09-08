"""AST 静态扫描：导入即失败。

不支持的 API 直接列出行号报错，绝不静默返回错误结果。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from lquant.research.dialect.mapping import UNSUPPORTED, WARNINGS


def scan(path: str) -> list[str]:
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in UNSUPPORTED:
                problems.append(f"L{node.lineno}: {name}() 暂不支持（需改写成原生 on_bar）")
            elif name in WARNINGS:
                problems.append(f"L{node.lineno}: [警告] {name} — {WARNINGS[name]}")
    return problems


def main(argv: list[str]) -> int:
    for p in argv[1:]:
        probs = scan(p)
        if probs:
            print(f"{p}:")
            for x in probs:
                print("  " + x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
