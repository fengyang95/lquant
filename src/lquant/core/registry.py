"""通用注册表 + 自省。

贯穿所有成熟仓库的模式：新增能力不改上层，且前端能自动枚举出 UI。
算子注册表 / 预处理方法注册表 / Provider 注册表 都用它。
"""
from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, name: str) -> None:
        self.name = name
        self._items: dict[str, T] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def register(self, key: str, meta: dict[str, Any] | None = None) -> Callable[[T], T]:
        def deco(fn: T) -> T:
            if key in self._items:
                raise ValueError(f"{self.name}: 重复注册 {key}")
            self._items[key] = fn
            self._meta[key] = meta or {}
            return fn

        return deco

    def __call__(self, key: str, meta: dict[str, Any] | None = None) -> Callable[[T], T]:
        return self.register(key, meta)

    def get(self, key: str) -> T:
        if key not in self._items:
            raise KeyError(f"{self.name}: 未注册 {key}，可选: {sorted(self._items)}")
        return self._items[key]

    def meta(self, key: str) -> dict[str, Any]:
        return self._meta.get(key, {})

    def keys(self) -> list[str]:
        return sorted(self._items)

    def describe(self) -> list[dict[str, Any]]:
        """自省：给前端枚举 UI 用。"""
        return [
            {"name": k, **self._meta.get(k, {})}
            for k in sorted(self._items)
        ]

    def __contains__(self, key: str) -> bool:
        return key in self._items
