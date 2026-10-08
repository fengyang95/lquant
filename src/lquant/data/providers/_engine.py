"""MappingProvider：通用数据源适配器引擎。

子类只需：
1. 设置 ``name``（与 ``source``，yaml sources key，缺省回落 name）
2. 实现 ``_fetch_raw``（返回源列名的 DataFrame）

列名映射 / 派生列 / 常量填充 / schema coerce / 质量断言全部由
mapping.yaml + 本引擎统一处理，避免每个 adapter 重复写归一化代码。
"""
from __future__ import annotations

from abc import abstractmethod
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from lquant.core.errors import MappingError
from lquant.data.base import DataProvider
from lquant.data.mapping import apply_mapping, load_table_mapping
from lquant.data.normalize import assert_ohlc, assert_plausible_prices
from lquant.data.schema import SCHEMAS, coerce, empty

# 需要执行价格/OHLC 质量断言的表
_ASSERT_TABLES = frozenset({"daily_bar", "minute_bar"})

# 允许作为 fill 覆盖注入 apply_mapping 的 params 白名单。
# 其余 params（symbols/start/end 等）只传给 _fetch_raw ——
# 否则恰为 schema 列名的 fetch 参数（如 symbol）会以 pl.lit 常量
# 覆盖真实数据列，多票数据被静默压扁。
_FILL_OVERRIDE_KEYS = frozenset({"freq", "sec_type"})


class MappingProvider(DataProvider):
    """基于 mapping.yaml 的 Provider 基类。"""

    # yaml sources key；为空时回落到 name
    source: str = ""

    @abstractmethod
    def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
        """拉取源始数据：返回**源列名**的 DataFrame，映射交给引擎。"""

    # ---- DataProvider 6 个核心方法的默认实现：具体 adapter 按能力覆写 ----
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 daily_bars")

    def minute_bars(
        self, symbols: list[str], start: date, end: date, freq: str
    ) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 minute_bars")

    def adj_factors(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 adj_factors")

    def financial_pit(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 financial_pit")

    def securities(self) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 securities")

    def trade_calendar(self, start: date, end: date) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 trade_calendar")

    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        """归一化后的钩子（补 source 列、单位换算等），默认原样返回。"""
        return df

    def _fetch(self, table: str, *args: Any, **params: Any) -> pl.DataFrame:
        """带**源级单飞锁**的取数：同一源同一时刻只允许一个会话在打。

        限流（TokenBucket）管速率，这把锁管并发会话 —— 东财/AKShare 的封禁
        与 BaoStock 的 10001011 都把「并发」列为触发条件。锁按源名区分，
        并与 watchdog 里的同一把锁嵌套复用（进程内计数，不会自锁）。
        """
        from lquant.data.ratelimit import source_lock

        with source_lock(self.source or self.name):
            return self._fetch_raw(table, *args, **params)

    def request(
        self,
        table: str,
        *args: Any,
        config_dir: str | Path | None = None,
        **params: Any,
    ) -> pl.DataFrame:
        """取数主流程：raw → mapping → coerce → 质量断言 → hook。

        params 分离：
        - ``_raw``：直接注入已取好的源数据（测试/缓存场景），不走 _fetch_raw
        - 白名单键（freq / sec_type）：作为 fill 覆盖传给 apply_mapping，
          仅当目标表确有该 schema 列；否则 fail-fast 抛 MappingError
        - 其余（symbols/start/end 等 canonical fetch 参数）：只传给 _fetch_raw，
          绝不进入 mapping fill，防止常量覆盖真实数据列
        ``config_dir`` 仅用于定位 mapping yaml，不参与 fill。
        """
        raw = params.pop("_raw") if "_raw" in params else self._fetch(
            table, *args, **params
        )
        # 空结果短路：零列/零行 raw 进映射管线会在 rename 处炸
        # （ColumnNotFoundError），统一短路成 schema 形状的空表
        if raw.width == 0 or raw.height == 0:
            return self._post_normalize(empty(table), table)
        tm = load_table_mapping(
            table, self.source or self.name, config_dir=config_dir
        )
        fill_overrides = {
            k: v for k, v in params.items()
            if k in _FILL_OVERRIDE_KEYS and k in SCHEMAS[table]
        }
        for k in _FILL_OVERRIDE_KEYS & params.keys():
            if k not in SCHEMAS[table]:
                raise MappingError(
                    f"{table} 无 {k!r} 列，不能作为 fill 覆盖传入 request"
                )
        out = apply_mapping(raw, tm, fill_overrides)
        out = coerce(out, table)
        if table in _ASSERT_TABLES:
            assert_plausible_prices(out)
            assert_ohlc(out)
        return self._post_normalize(out, table)
