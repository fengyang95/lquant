"""因子评价的**平台级默认口径** —— 单一事实来源。

三个报告入口（CLI ``lq factor report`` / API ``/factors/evaluate`` /
``/factors/synthesize``）此前各自写死自己的默认值，于是同一个生成器产出三种
报告：衰减期数 8 vs 4、分层组数 10 vs 5、成本档位各写一遍。用户看到的是
「同一平台两种口径」，而且报告里没有任何字段能说明用的是哪一种。

这里的常量是**契约**（见 ``docs/因子报告内容契约.md``）：入口只允许引用，
不允许再写字面量；``tests/unit/test_report_defaults.py`` 会直接比对三个入口的
默认值是否 === 这些常量，防止再次漂移。

为什么默认值是这些：
- ``DEFAULT_DECAY_HORIZONS``：衰减曲线的用途是回答「多久调一次仓」，
  半衰期要能被 40/60 日兜住，只算到 20 日时慢因子会得出「衰减不完」的假象。
- ``DEFAULT_N_GROUPS``：10 分位是因子评价的行业惯例（alphalens / qlib 同），
  也是报告与 CLI 一直以来的取值；API 曾用 5，是唯一的异类。
- ``DEFAULT_WINDOW``：滚动窗口 60 交易日 ≈ 一季度，与 API 的 series 端一致。
- ``DEFAULT_BPS``：0/5/10/15/30 覆盖「零成本假设 → A 股双边约 15bp」的决策区间。
"""

from __future__ import annotations

#: 分层组数（分位组数）。
DEFAULT_N_GROUPS = 10

#: 衰减曲线持有期（交易日）。含义见模块 docstring。
DEFAULT_DECAY_HORIZONS: tuple[int, ...] = (1, 2, 3, 5, 10, 20, 40, 60)

#: 滚动 IC 窗口（交易日）。
DEFAULT_WINDOW = 60

#: 成本敏感性档位（单边 bps）。
DEFAULT_BPS: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 30.0)

#: 事件式分层收益的窗口（事件日前, 事件日后）。
DEFAULT_EVENT_WINDOW: tuple[int, int] = (10, 15)


def decay_horizons(h: list[int] | None = None) -> list[int]:
    """解析衰减阶梯：``None`` / 空 → 平台默认；显式传入则原样（去重保序）。"""
    if not h:
        return list(DEFAULT_DECAY_HORIZONS)
    seen: list[int] = []
    for x in h:
        v = int(x)
        if v not in seen:
            seen.append(v)
    return seen


def horizons_csv() -> str:
    """衰减阶梯的 CLI 展示形式（``1,2,3,5,10,20,40,60``）。"""
    return ",".join(str(h) for h in DEFAULT_DECAY_HORIZONS)


__all__ = [
    "DEFAULT_BPS",
    "DEFAULT_DECAY_HORIZONS",
    "DEFAULT_EVENT_WINDOW",
    "DEFAULT_N_GROUPS",
    "DEFAULT_WINDOW",
    "decay_horizons",
    "horizons_csv",
]
