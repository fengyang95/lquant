"""词法分析。支持 $close、数字、算子名、+-*/()、逗号。"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(
    r"""
    (?P<field>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<number>\d+\.?\d*(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>[-+*/(),<>])
  | (?P<ws>\s+)
    """,
    re.VERBOSE,
)


def tokenize(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise SyntaxError(f"无法识别的字符 @ {pos}: {src[pos:pos + 10]!r}")
        kind = m.lastgroup
        text = m.group()
        pos = m.end()
        if kind == "ws":
            continue
        tokens.append((kind or "op", text))
    return tokens
