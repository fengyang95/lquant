"""MappingProvider：通用数据源适配器引擎。

子类只需：
1. 设置 ``name``（与 ``source``，yaml sources key，缺省回落 name）
2. 实现 ``_fetch_raw``（返回源列名的 DataFrame）

列名映射 / 派生列 / 常量填充 / schema coerce / 质量断言全部由
mapping.yaml + 本引擎统一处理，避免每个 adapter 重复写归一化代码。
"""
from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Any

import polars as pl

from lquant.data.base import DataProvider
from lquant.data.mapping import apply_mapping, load_table_mapping
from lquant.data.normalize import assert_ohlc, assert_plausible_prices
from lquant.data.schema import coerce

# 需要执行价格/OHLC 质量断言的表
_ASSERT_TABLES = frozenset({"daily_bar", "minute_bar"})


class MappingProvider(DataProvider):
    """基于 mapping.yaml 的 Provider 基类。"""

    # yaml sources key；为空时回落到 name
    source: str = ""

    @abstractmethod
    def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
        """拉取源始数据：返回**源列名**的 DataFrame，映射交给引擎。"""

    # ---- DataProvider 6 个核心方法的默认实现：具体 adapter 按能力覆写 ----
    def daily_bars(self, symbols: list[str], start, end) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 daily_bars")

    def minute_bars(self, symbols: list[str], start, end, freq: str) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 minute_bars")

    def adj_factors(self, symbols: list[str], start, end) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 adj_factors")

    def financial_pit(self, symbols: list[str], start, end) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 financial_pit")

    def securities(self) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 securities")

    def trade_calendar(self, start, end) -> pl.DataFrame:
        raise NotImplementedError(f"{self.name} 未实现 trade_calendar")

    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        """归一化后的钩子（补 source 列、单位换算等），默认原样返回。"""
        return df

    def request(
        self,
        table: str,
        *args: Any,
        config_dir: str | Path | None = None,
        **params: Any,
    ) -> pl.DataFrame:
        """取数主流程：raw → mapping → coerce → 质量断言 → hook。

        params 中的 freq / sec_type 等键会覆盖 yaml fill；
        ``_raw`` 参数可直接注入已取好的源数据（测试/缓存场景）；
        ``config_dir`` 仅用于定位 mapping yaml，不参与 fill。
        """
        raw = params.pop("_raw") if "_raw" in params else self._fetch_raw(
            table, *args, **params
        )
        tm = load_table_mapping(
            table, self.source or self.name, config_dir=config_dir
        )
        out = apply_mapping(raw, tm, params)
        out = coerce(out, table)
        if table in _ASSERT_TABLES:
            assert_plausible_prices(out)
            assert_ohlc(out)
        return self._post_normalize(out, table)
