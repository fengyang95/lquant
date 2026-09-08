"""因子引擎：load data → compute → preprocess → evaluate。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.errors import LookaheadError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.compiler import compile_expr, plan
from lquant.factors.dsl.parser import parse


class FactorEngine:
    def __init__(self, data: pl.LazyFrame) -> None:
        self.data = data

    def compute(self, expr: str, name: str = "f") -> pl.DataFrame:
        ast = parse(expr, name)
        check(ast)   # 静态分析：未来函数 + 未注册算子

        steps = plan(ast.root)
        df = self.data.collect()
        for i, step in enumerate(steps):
            e = compile_expr(step)
            col = f"__step{i + 1}" if len(steps) > 1 else name
            df = df.with_columns(e.alias(col))
            if len(steps) > 1:
                # 物化：打断 Polars 的嵌套 over 优化（必须）
                df = df.with_columns(pl.col(col).alias(col))
        if len(steps) > 1:
            df = df.rename({f"__step{len(steps)}": name})
        return df

    def compute_many(self, defs: list[dict], start: date, end: date) -> pl.DataFrame:
        raise NotImplementedError("见 M4：DAG 调度 + 两级缓存")
