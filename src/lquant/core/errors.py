"""统一异常。分层异常让上层能区分「该重试」还是「该换源」。"""
from __future__ import annotations


class LQuantError(Exception):
    """所有异常的基类。"""


class ConfigError(LQuantError):
    pass


# ---- 数据接入 ----
class DataError(LQuantError):
    """数据层异常基类。"""


class SourceUnavailable(DataError):
    """源站不可达 / 被封 / 超时。可重试、可切源。"""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason


class DataUnavailable(SourceUnavailable):
    """看板/采集器语义：当日该数据不可得（未开盘、源无该日期）。"""


class SourceSchemaChanged(DataError):
    """源站改版，字段消失。这是永久错误，重试无意义。"""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider} schema changed: {detail}")
        self.provider = provider


class CapabilityMissing(DataError):
    """源根本不支持该能力（如某源没有 1 分钟线）。

    必须显式报错，绝不静默降级或返回空 —— 静默会导致「回测跑通了但数据不对」。
    """

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"{provider} does not support {capability}")
        self.provider = provider
        self.capability = capability


class MappingError(DataError):
    """源字段映射配置非法（rename/derive/fill 目标不在 schema 内、表达式越权等）。"""


class DataQualityError(DataError):
    """质量断言失败。"""

    def __init__(self, rule: str, detail: str, severity: str = "fatal") -> None:
        super().__init__(f"[{severity}] {rule}: {detail}")
        self.rule = rule
        self.severity = severity


# ---- 因子 / 回测 ----
class FactorError(LQuantError):
    pass


class DSLParseError(FactorError):
    pass


class LookaheadError(FactorError):
    """检测到未来函数。"""


class BacktestError(LQuantError):
    pass


class RuleNotFound(BacktestError):
    pass


# ---- 机器学习研究链路 ----
class MLError(LQuantError):
    """ML 研究链路的基类异常（数据集/处理器/训练/注册表）。"""


class LeakageError(MLError):
    """特征工程或切分上的未来函数：fit 窗口看到了不该看到的样本。"""
