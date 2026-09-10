"""领域类型。

三条硬约束在这里落地：
1. Symbol 统一 `000001.SZ` / `510300.SH` —— 绝不用裸 6 位
2. 时间统一 ISO 8601 + Asia/Shanghai
3. 金额统一元
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# 交易所代码段。注意 adata 的 exchange_suffix 表不含 ETF 段，这里必须自己补。
_SUFFIX_RULES = [
    (re.compile(r"^(60|68|51|56|58|50|11|5[0-9])\d{4}$"), "SH"),   # 沪股 + 沪 ETF/LOF
    (re.compile(r"^(00|30|15|16|159|12|18)\d{3,4}$"), "SZ"),       # 深股 + 深 ETF/LOF
    (re.compile(r"^(4|8|92)\d{4}$"), "BJ"),
]
_SYMBOL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


class SecType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    LOF = "lof"
    BOND = "bond"


class Board(StrEnum):
    MAIN = "main"
    GEM = "gem"        # 创业板 300/301
    STAR = "star"      # 科创板 688
    BSE = "bse"        # 北交所
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Symbol:
    """`000001.SZ` —— 裸 `000001` 是歧义码（上证指数 vs 平安银行）。"""

    code: str
    exchange: str

    def __str__(self) -> str:
        return f"{self.code}.{self.exchange}"

    @property
    def sec_type(self) -> SecType:
        c = self.code
        if self.exchange == "SH":
            if c.startswith(("51", "58", "56", "50")):
                return SecType.ETF
            if c.startswith("5"):
                return SecType.LOF
        if self.exchange == "SZ" and c.startswith(("15", "16", "159")):
            return SecType.ETF
        if c in ("000001", "000300", "000905", "000852") and self.exchange == "SH":
            return SecType.INDEX
        if c.startswith("399") and self.exchange == "SZ":
            return SecType.INDEX
        return SecType.STOCK

    @property
    def board(self) -> Board:
        c = self.code
        if c.startswith("688"):
            return Board.STAR
        if c.startswith(("300", "301", "302")):
            return Board.GEM
        if self.exchange == "BJ":
            return Board.BSE
        if c.startswith(("60", "00")):
            return Board.MAIN
        return Board.UNKNOWN


def parse_symbol(raw: str) -> Symbol:
    """归一各种输入格式 → Symbol。

    支持：600000 / 600000.SH / sh.600000 / 600000.XSHG
    """
    s = raw.strip().upper()
    m = _SYMBOL_RE.match(s)
    if m:
        return Symbol(m.group(1), m.group(2))
    # 支持 600000.SH（后缀式）与 sh.600000 / SH.600000（前缀式，BaoStock 用这种）
    if "." in s:
        a, b = s.rsplit(".", 1)
        b = b.replace("XSHG", "SH").replace("XSHE", "SZ").replace("XBEI", "BJ")
        if b in ("SH", "SZ", "BJ") and a.isdigit() and len(a) == 6:
            return Symbol(a, b)
        a = a.replace("XSHG", "SH").replace("XSHE", "SZ").replace("XBEI", "BJ")
        if a in ("SH", "SZ", "BJ") and b.isdigit() and len(b) == 6:
            return Symbol(b, a)
    if s.isdigit() and len(s) == 6:
        for pat, ex in _SUFFIX_RULES:
            if pat.match(s):
                return Symbol(s, ex)
    raise ValueError(f"无法解析标的代码: {raw!r}")


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start({self.start}) > end({self.end})")


def now_cn() -> datetime:
    return datetime.now(TZ)


def today_cn() -> date:
    return now_cn().date()
