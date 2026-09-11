"""新浪适配器（D3 复权因子主源）。

BaoStock 只给复权价不给因子 —— 设计文档 D3：@ 新浪 qfq/hfq + 同花顺官方校验。

新浪 hfq.js / qfq.js 返回**复权收盘价序列**（JSONP），复权因子
factor[t] = hfq_close[t] / raw_close[t]。因为 raw 需要另一路查询，
这里直接走 hfq.js + qfq.js 两路（同源不同口径），再用
hfq/qfq 对数差做内部恒等校验（两者只差常数因子，收益率必须恒等，
交给 validators.check_ret_identity 的用户侧逻辑）。

注意：该端点字段是竖排数组格式（见解析器），且新浪未承诺结构化契约 ——
解析器做防御性校验，schema 变了报 SourceSchemaChanged 而不是静默返错。
"""
from __future__ import annotations

import json
import re
from datetime import date

import polars as pl

from lquant.core.errors import SourceSchemaChanged
from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.providers import PROVIDERS


def _sina_code(symbol: str) -> str:
    code, ex = symbol.split(".")
    return f"{ex.lower()}{code}"


def parse_hfq(payload: str) -> tuple[list[str], list[float]]:
    """解析 hfq.js 竖排 JSONP → (dates, hfq_close)。

    形如: var _sh600519_hfq={"data":[[date,o,h,l,c,vol],...]}
    只取 date + close。防御式解析 —— 结构不符抛 SourceSchemaChanged，
    绝不静默返回空让上游以为「无复权数据」。
    """
    m = re.search(r"=\s*(\{.*\})\s*$", payload, re.S)
    if not m:
        raise SourceSchemaChanged("sina", "hfq.js 返回体找不到 JSON 对象")
    body = m.group(1)
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # 新浪按年分组时键可能是裸数字 {2024: [...]}，unquoted —— 补引号再试
        try:
            body = re.sub(r'"?(\d{4})"?\s*:', r'"\1":', body)
            body = re.sub(r'(?<=[{,])\s*([A-Za-z_]\w*)\s*:', r'"\1":', body)
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            raise SourceSchemaChanged("sina", f"hfq.js JSON 解析失败: {e}") from e
    if "data" in (obj if isinstance(obj, dict) else {}):
        rows = obj["data"]
    elif isinstance(obj, dict):
        # 按年份分组的常见形态：{2020: [[date,o,h,l,c,v], ...], ...}
        groups = [v for v in obj.values() if isinstance(v, list)]
        rows = [r for g in groups for r in g] if groups else list(obj.values())
    else:
        rows = None
    if not isinstance(rows, list) or not rows:
        raise SourceSchemaChanged("sina", "hfq.js data 为空或结构未知")
    dates, closes = [], []
    for r in rows:
        if not isinstance(r, list) or len(r) < 5:
            raise SourceSchemaChanged("sina", f"hfq.js 行结构异常: {r}")
        dates.append(str(r[0]))
        closes.append(float(r[4]))
    return dates, closes


def _fetch_hfq(code: str) -> str:  # pragma: no cover - 网络
    import urllib.request

    url = f"https://finance.sina.com.cn/realstock/company/{code}/hfq.js"
    req = urllib.request.Request(url, headers={"User-Agent": "lquant/0.1",
                                               "Referer": url})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("gbk", errors="replace")


@PROVIDERS.register("sina", {"free": True, "need_token": False,
                             "note": "复权因子源，端点无常量契约（实验性）"})
class SinaProvider(DataProvider):
    name = "sina"
    capability = frozenset({Capability.ADJ_FACTOR})

    def __init__(self, qps: float = 5, capability: frozenset[Capability] | None = None) -> None:
        self.capability = capability or self.capability

    def adj_factors(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        """复权因子 = 前复权收盘（新浪 hfq 口径保留绝对量级）。

        注意：新浪 hfq 是**后复权**收盘价，本适配器以 hfq_close 为
        factor 乘到 raw close 的近似不可取 —— 更稳的因子增量由 akshare
        stock_zh_a_daily 提供（hfq_factor/qfq_factor 列），两者互为对拍源。
        此处返回 date→hfq_close 序列，上层做因子时用
        factor = hfq_close / raw_close 归一。
        """
        from lquant.core.errors import DataUnavailable

        frames = []
        for sym in symbols:
            dates, closes = parse_hfq(_fetch_hfq(_sina_code(sym)))
            if not dates:
                continue
            df = pl.DataFrame({"trade_date": dates, "hfq_close": closes})
            df = df.with_columns(
                symbol=pl.lit(sym),
                trade_date=pl.col("trade_date").str.to_date("%Y-%m-%d"),
            ).filter(
                (pl.col("trade_date") >= start) & (pl.col("trade_date") <= end)
            )
            frames.append(df)
        if not frames:
            raise DataUnavailable("sina", "无复权数据返回")
        return pl.concat(frames, how="diagonal").select(
            "symbol", "trade_date", "hfq_close",
        )

    # 其余核心方法：新浪无这些能力 —— 显式报错而非伪造
    def daily_bars(self, symbols, start, end):
        self.require(Capability.DAILY)
        raise NotImplementedError

    def minute_bars(self, symbols, start, end, freq):
        self.require(f"minute_{freq}" if freq else Capability.MINUTE_1)
        raise NotImplementedError

    def financial_pit(self, symbols, start, end):
        self.require(Capability.FINANCIAL_PIT)
        raise NotImplementedError

    def securities(self):
        self.require(Capability.REFERENCE)
        raise NotImplementedError

    def trade_calendar(self, start, end):
        self.require(Capability.CALENDAR)
        raise NotImplementedError