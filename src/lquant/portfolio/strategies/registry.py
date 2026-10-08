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

import inspect
from dataclasses import dataclass

import polars as pl

from lquant.core.registry import Registry

__all__ = ["STRATEGIES", "register_strategy", "StrategyResult", "run_strategy", "run_all"]


class _CopyingMetaRegistry(Registry):
    """``meta()`` 返回副本的注册表。

    core.Registry.meta 把内部 dict **本体**交出去，外部一句
    ``STRATEGIES.meta("volume_surge")["min_rows"] = 999`` 就能永久改掉判定
    参数且无痕。core 是共享模块不能动，所以在策略注册表层覆写 meta 做隔离；
    ``describe()`` 仍读内部 ``_meta``，自省输出不受影响。
    """

    def meta(self, key: str) -> dict:
        return dict(super().meta(key))


STRATEGIES = _CopyingMetaRegistry("strategies")


@dataclass(frozen=True)
class StrategyResult:
    """单策略判定结果。passed=False 时 evidence 说明差距，绝不静默。"""

    strategy: str
    passed: bool
    evidence: str
    data_insufficient: bool = False


def register_strategy(name: str, label: str, desc: str, min_rows: int):
    """注册装饰器。meta 随 Registry.describe() 输出，供 API/前端枚举。

    同名重复注册按「替换」处理而非报错：``rules`` 模块被 ``importlib.reload``
    或热载重新执行时会再次走到本函数，而 ``core.Registry.register`` 对重复 key
    直接 raise——那会让整条热载路径崩掉。core/registry.py 是共享模块不动，
    所以在注册前主动清掉旧项，把替换语义收敛在 portfolio/strategies 内部。
    """
    if name in STRATEGIES:
        STRATEGIES._items.pop(name, None)
        STRATEGIES._meta.pop(name, None)
    return STRATEGIES.register(name, {"label": label, "desc": desc, "min_rows": min_rows})


def _spec(name: str) -> dict:
    """返回 meta 的**拷贝**：注册表内部 dict 一旦外泄，调用方改一个字段就
    永久改变所有后续判定行为（且无痕），必须隔离。"""
    return dict(STRATEGIES.meta(name))


def _ok(name: str, evidence: str) -> StrategyResult:
    return StrategyResult(name, True, evidence)


def _no(name: str, evidence: str, *, insufficient: bool = False) -> StrategyResult:
    return StrategyResult(name, False, evidence, data_insufficient=insufficient)


def _need_rows(name: str, df: pl.DataFrame, need: int, reason: str) -> StrategyResult | None:
    """按**实际运行参数**复算行数需求：不足返回显式降级结果，够则 None。

    注册时的 ``min_rows`` 是标量下界，看不到调用方传的 ``days``/``window``；
    若只靠它，参数调大后实现会拿半截数据硬算（静默错数）或 IndexError
    （内部崩溃当 evidence 暴露）。所以每条依赖参数的规则必须在实现内部自校。
    """
    if df.height >= need:
        return None
    return _no(name,
               f"数据不足: {df.height} 行 < 需要 {need} 行"
               f"（{reason}，缺 {need - df.height} 行）",
               insufficient=True)


def _guard(name: str, df: pl.DataFrame, **params) -> StrategyResult:
    """公共前置：行数够不够。数据不足是显式降级，不是 False。

    ``params`` 按目标函数签名过滤（如 ``min_amount`` 只有 volume_surge 认）：
    ``run_all(df, **common_params)`` 的公共参数不该打到无此形参的策略上 ——
    否则 TypeError 会被下面的 except 吞成「计算失败」，表面不崩实则功能坏。
    """
    min_rows = int(_spec(name).get("min_rows", 0))
    if df.height < min_rows:
        return _no(name, f"数据不足: {df.height} 行 < 需要 {min_rows} 行", insufficient=True)
    # 取注册表里存好的函数对象，而不是按 `_run_<name>` 命名约定去模块里反查：
    # 后者在别名注册（名字 ≠ 函数名）时会让策略「注册成功但静默不可用」。
    fn = STRATEGIES.get(name)
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
