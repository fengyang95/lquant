"""因子引擎：load data → compute → compute_many(DAG+缓存) → preprocess → evaluate。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.compiler import compile_expr, plan
from lquant.factors.dsl.parser import parse


class FactorEngine:
    def __init__(self, data: pl.LazyFrame) -> None:
        self.data = data

    def _compute_column(self, df: pl.DataFrame, expr: str, name: str) -> pl.DataFrame:
        """在已有 panel 上算一个因子列，返回把该列加进 df 的结果。

        公因子 compute 与 compute_many 的逐因子求值。多算子表达式要按
        plan 分步物化（打断 Polars 的嵌套 over 优化），命名用因子名前缀
        避免多因子共用 panel 时中间列互相覆盖。

        先过 ``normalize``：库里历史遗留的 qlib 写法（Slope/Mean/Ref…）在这里
        翻译成 DSL 再算，否则「打开得了、算不出来」会在评价阶段才炸。
        """
        from lquant.factors.dsl.normalize import normalize

        expr = normalize(expr)
        ast = parse(expr, name)
        check(ast, allowed_fields=set(df.columns))   # 静态分析：未来函数 + 未注册算子 + 字段白名单

        steps = plan(ast.root)
        if not steps:
            raise FactorError(f"表达式编译出空执行计划: {expr}")
        pref = f"{name}__s" if len(steps) > 1 else name
        out = df
        for i, step in enumerate(steps):
            e = compile_expr(step)
            col = f"{pref}{i + 1}" if len(steps) > 1 else name
            out = out.with_columns(e.alias(col))
            if len(steps) > 1:
                # 物化：打断 Polars 的嵌套 over 优化（必须）；
                # 同时 alias 成 plan 的引用名 __step{i+1}，供后续步骤读取
                out = out.with_columns(pl.col(col).alias(col))
                out = out.with_columns(pl.col(col).alias(f"__step{i + 1}"))
        if len(steps) > 1:
            inter = ([f"{pref}{i + 1}" for i in range(len(steps) - 1)]
                     + [f"__step{i + 1}" for i in range(len(steps))])
            # rename 到已存在的列名会抛 DuplicateError —— 与单步路径的
            # with_columns 覆盖语义不一致（robust 的 train 帧自带上一轮
            # prepare_segment 算出的 f 列，正是这么崩的）。先丢旧列，保证
            # 「同名列 = 覆盖重算」在两条路径上行为一致。
            if name in out.columns:
                out = out.drop(name)
            out = out.rename({f"{pref}{len(steps)}": name}).drop(inter)
        return out

    def compute(self, expr: str, name: str = "f") -> pl.DataFrame:
        return self._compute_column(self.data.collect(), expr, name)

    def compute_many(self, defs: list[dict], start: date, end: date,
                     *, steps: list[dict] | None = None,
                     data_version: str | None = None) -> pl.DataFrame:
        """批量计算多因子（DAG：一次 collect + 一次共享预处理）+ 两级缓存。

        defs : [{name, expression}]
        start/end : 窗口，用于缓存 key（PIT-安全的前提：数据已限窗，不会用到未来）
        steps : 可选预处理流水线；给定时在全部因子算完后统一跑一遍，再写缓存。

        实现：
          1. cache.key(defs, start, end, data_version) 查两级缓存 → 命中直返
          2. miss：collect 一次 → 逐因子 _compute_column 累加列
             （所有因子共享同一个 base panel，算子子表达式只在需要处求值）
          3. 可选跑一遍预处理流水线（跨因子统一，正交化需要多列）
          4. 写两级缓存
        """
        from lquant.data import lineage
        from lquant.factors.cache import get_cache
        from lquant.factors.preprocess.pipeline import run as pipeline_run

        version = data_version or lineage.latest("daily") or "unknown"
        cache = get_cache()
        key = cache.key(defs, start, end, version, steps=steps)
        hit = cache.get(key)
        if hit is not None:
            return hit

        base = self.data.collect()
        names: list[str] = []
        for d in defs:
            base = self._compute_column(base, d["expression"], d["name"])
            names.append(d["name"])

        if steps:
            base = pipeline_run(base, cols=names, steps=steps)

        cache.set(key, base)
        return base