"""Provider 注册表与工厂。"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from lquant.core.config import load_providers
from lquant.core.registry import Registry
from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.fallback import FallbackProvider, HealthTracker

PROVIDERS: Registry[type[DataProvider]] = Registry("providers")


def _import_all() -> None:
    import contextlib  # noqa: PLC0415
    import importlib  # noqa: PLC0415

    for mod in ("baostock", "akshare", "efinance", "hithink", "sina", "tencent", "tushare"):
        # 已注册的源跳过重复 import：sys.modules 被测试 pop 后再 import
        # 会让模块级 @PROVIDERS.register 装饰器重复注册炸掉
        if mod in PROVIDERS:
            continue
        try:
            importlib.import_module(f"lquant.data.providers.{mod}")
        except ImportError:
            continue
    with contextlib.suppress(ImportError):
        importlib.import_module("lquant.market.providers.mootdx")


@lru_cache(maxsize=1)
def build_chain() -> FallbackProvider:
    """按 config/providers.yaml 顺序构建 Fallback 链。

    构建前对 config/schema/*.yaml 全量静态校验：映射错误启动即抛
    （fail-fast），绝不带着坏映射静默取数。
    """
    from lquant.core.config import get_settings  # noqa: PLC0415
    from lquant.data.mapping import validate_all_mappings  # noqa: PLC0415

    validate_all_mappings(get_settings().config_dir)
    _import_all()
    cfg = load_providers()
    chain: list[DataProvider] = []
    for item in cfg.get("providers", []):
        if not item.get("enabled", False):
            continue
        # token 门控：声明了 env_key 但环境变量缺失 → 跳过该源（不抛错），
        # 让 fallback 链在无凭证环境下也能用剩余源构建。
        env_key = item.get("env_key")
        if env_key and env_key not in os.environ:
            logging.warning(
                "provider %s 已启用但缺少环境变量 %s，跳过", item["name"], env_key
            )
            continue
        name = item["name"]
        if name not in PROVIDERS:
            # 可选 SDK 未安装（如 mootdx）→ 降级跳过而非炸启动，与 token 门控同哲学
            logging.warning("provider %s 已启用但未注册（SDK 未安装？），跳过", name)
            continue
        cls = PROVIDERS.get(name)
        caps = frozenset(Capability.parse(c) for c in item.get("capability", []))
        chain.append(cls(qps=item.get("qps", 1), capability=caps))   # type: ignore[call-arg]
    if not chain:
        raise RuntimeError("没有启用任何 provider，检查 config/providers.yaml")
    return FallbackProvider(chain, HealthTracker())


def get_provider() -> FallbackProvider:
    return build_chain()


def reset_chain() -> None:
    """清缓存，便于测试中重配 provider。"""
    build_chain.cache_clear()


__all__ = ["PROVIDERS", "get_provider", "build_chain", "reset_chain"]
