"""Provider 注册表与工厂。"""
from __future__ import annotations

from functools import lru_cache

from lquant.core.config import load_providers
from lquant.core.registry import Registry
from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.fallback import FallbackProvider, HealthTracker

PROVIDERS: Registry[type[DataProvider]] = Registry("providers")


def _import_all() -> None:
    import importlib

    for mod in ("baostock", "akshare", "efinance", "hithink", "sina", "tencent"):
        try:
            importlib.import_module(f"lquant.data.providers.{mod}")
        except ImportError:
            continue
    try:
        importlib.import_module("lquant.market.providers.mootdx")
    except ImportError:
        pass


@lru_cache(maxsize=1)
def build_chain() -> FallbackProvider:
    """按 config/providers.yaml 顺序构建 Fallback 链。"""
    from lquant.data.ratelimit import TokenBucket

    _import_all()
    cfg = load_providers()
    chain: list[DataProvider] = []
    for item in cfg.get("providers", []):
        if not item.get("enabled", False):
            continue
        cls = PROVIDERS.get(item["name"])
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
