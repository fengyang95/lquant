"""选股策略注册表：一策略一纯函数 + evidence（借鉴 InStock，按 lquant 风格改造）。

InStock（14.7k star）的约定是每个策略一个 ``check_xxx(code_name, data, date)``
纯函数、规则写在文件头注释。本目录沿其形、改其质：

- **Polars 列式实现**，输入统一为单标的日线 ``pl.DataFrame``
  （列：trade_date/open/high/low/close/volume/amount/pre_close，按日升序）；
- **返回 evidence 而非裸 bool**：命中给数字依据，未命中也给差距 ——
  lquant 的「不静默」哲学：报告里要能解释为什么入选/落选；
- **数据不足显式降级**（``data_insufficient``），不拿半截数据硬算；
- 与 ``indicators/registry`` 同构的注册表模式（core.Registry），前端/API 可
  ``describe()`` 自动枚举。

用法::

    from lquant.portfolio.strategies import run_strategy, run_all
    res = run_strategy("volume_surge", df)   # 单策略
    for r in run_all(df): ...                # 全策略扫
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from lquant.core.registry import Registry

__all__ = ["STRATEGIES", "register_strategy", "StrategyResult", "run_strategy", "run_all"]


STRATEGIES = Registry("strategies")


@dataclass(frozen=True)
class StrategyResult:
    """单策略判定结果。passed=False 时 evidence 说明差距，绝不静默。"""

    strategy: str
    passed: bool
    evidence: str
    data_insufficient: bool = False


def register_strategy(name: str, label: str, desc: str, min_rows: int):
    """注册装饰器。meta 随 Registry.describe() 输出，供 API/前端枚举。"""
    return STRATEGIES.register(name, {"label": label, "desc": desc, "min_rows": min_rows})


def _spec(name: str) -> dict:
    return STRATEGIES.meta(name)


def _ok(name: str, evidence: str) -> StrategyResult:
    return StrategyResult(name, True, evidence)


def _no(name: str, evidence: str, *, insufficient: bool = False) -> StrategyResult:
    return StrategyResult(name, False, evidence, data_insufficient=insufficient)


def _guard(name: str, df: pl.DataFrame, **params) -> StrategyResult:
    """公共前置：行数够不够。数据不足是显式降级，不是 False。

    ``params`` 按目标函数签名过滤（如 ``min_amount`` 只有 volume_surge 认）：
    ``run_all(df, **common_params)`` 的公共参数不该打到无此形参的策略上 ——
    否则 TypeError 会被下面的 except 吞成「计算失败」，表面不崩实则功能坏。
    """
    min_rows = int(_spec(name).get("min_rows", 0))
    if df.height < min_rows:
        return _no(name, f"数据不足: {df.height} 行 < 需要 {min_rows} 行", insufficient=True)
    try:
        import inspect

        from lquant.portfolio.strategies import rules as _rules_mod

        fn = getattr(_rules_mod, f"_run_{name}")
    except (ImportError, AttributeError) as e:  # pragma: no cover
        return _no(name, f"策略实现缺失: {e}")
    known = inspect.signature(fn).parameters
    params = {k: v for k, v in params.items() if k in known}
    try:
        return fn(df, **params)
    except Exception as e:  # noqa: BLE001  策略算炸也要给出原因而不是静默消失
        return _no(name, f"计算失败: {type(e).__name__}: {e}")


def run_strategy(name: str, df: pl.DataFrame, **params) -> StrategyResult:
    """按名跑单策略。未知策略名显式报错（对齐 notify 的 _UnknownChannel 纪律）。"""
    if name not in STRATEGIES:
        raise KeyError(f"未知策略 {name}，可用: {', '.join(STRATEGIES.keys())}")
    return _guard(name, df, **params)


def run_all(df: pl.DataFrame, **params) -> list[StrategyResult]:
    """跑全部注册策略（互不干扰：单策略失败不影响其他）。"""
    return [run_strategy(name, df, **params) for name in STRATEGIES]
