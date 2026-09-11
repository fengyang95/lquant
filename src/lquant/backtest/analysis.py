"""自定义分析片段：沙箱 exec，输出 chart/table spec，前端动态渲染。"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl

from lquant.backtest.sandbox import safe_builtins
from lquant.backtest.validation import validate_source


class AnalysisError(ValueError):
    """分析片段校验/执行/输出 schema 失败。"""


def _ns() -> dict:
    ns = {"__name__": "__analysis__",
          "math": math, "statistics": statistics,
          "date": date, "datetime": datetime, "timedelta": timedelta,
          "Counter": Counter, "defaultdict": defaultdict, "deque": deque,
          "np": np, "pd": pd, "pl": pl}
    # 不设 __builtins__ 时 CPython 会自动注入**完整**内建（于是 __import__ 可用，
    # import 白名单形同虚设）。这里显式换成受限集合。
    ns["__builtins__"] = safe_builtins()
    return ns


def _validate_specs(specs) -> list[dict]:
    if not isinstance(specs, list):
        raise AnalysisError("analyze 必须返回 list[dict]")
    out = []
    for i, s in enumerate(specs):
        if not isinstance(s, dict) or s.get("type") not in ("chart", "table"):
            raise AnalysisError(f"分析输出[{i}] type 必须是 chart|table")
        if s["type"] == "chart" and not ({"data", "x", "ys"} <= set(s)):
            raise AnalysisError(f"分析输出[{i}] chart 需要 data/x/ys")
        if s["type"] == "table" and not ({"columns", "rows"} <= set(s)):
            raise AnalysisError(f"分析输出[{i}] table 需要 columns/rows")
        out.append({"type": s["type"], "title": str(s.get("title", "自定义分析")),
                    **{k: s[k] for k in s if k not in ("type", "title")}})
    return out


def run_user_analysis(source: str, payload: dict) -> list[dict]:
    errs = validate_source(source, require_initialize=False)
    if errs:
        raise AnalysisError("；".join(errs))
    try:
        ns = _ns()
        exec(source, ns)                       # noqa: S102 - 设计前提（同 jqapi）
        fn = ns.get("analyze")
        if fn is None:
            raise AnalysisError("必须定义 analyze(result) 函数")
        specs = fn(payload)
    except AnalysisError:
        raise
    except Exception as e:                     # noqa: BLE001
        raise AnalysisError(f"分析执行失败: {e}") from e
    return _validate_specs(specs)
