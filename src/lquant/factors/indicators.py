"""技术指标（方案 M3，P0）—— **兼容转发层**。

指标实体已迁到 :mod:`lquant.indicators`（注册表驱动、单一实现源）。
本模块只保留原有导入路径与函数签名，避免下游（API / 测试 / 外部脚本）改动。
新代码请直接 ``from lquant.indicators import ...``。

保留原因：``lquant.server.api.data`` 的 ``/api/data/indicators`` 与既有单测
都按此路径导入；一次性改路径的收益低于破坏成本。
"""
from __future__ import annotations

from lquant.indicators.composite import WARMUP, add_all
from lquant.indicators.momentum import add_rsi
from lquant.indicators.trend import add_boll, add_ema, add_ma, add_macd

__all__ = ["WARMUP", "add_all", "add_boll", "add_ema", "add_ma", "add_macd", "add_rsi"]
