"""LLM 提案接入（M3e/M5，平台驱动模式）。

LLM 只产表达式字符串（写在 JSONL 文件里），所有计算归平台 ——
与 GP/random 同一条门禁管线，同一种记账。字段: {expr, note?}。
"""
from __future__ import annotations

import json
from pathlib import Path


def load_proposals(path: str | Path) -> list[dict]:
    """读 JSONL 提案文件；格式错 fail-fast。"""
    fp = Path(path)
    if not fp.exists():
        raise FileNotFoundError(f"提案文件不存在: {fp}")
    out = []
    for i, line in enumerate(fp.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            assert "expr" in obj
            out.append(obj)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"提案第 {i+1} 行格式错误: {e}") from e
    return out


def make_generator(proposals: list[dict]):
    """把提案列表包成 runner 兼容的 generator 闭包。"""
    it = iter([p["expr"] for p in proposals])

    def gen():
        try:
            return next(it)
        except StopIteration:
            raise StopIteration from None
    return gen
