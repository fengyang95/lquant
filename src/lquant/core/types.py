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
    (re.compile(r"^(60|68|51|56|58|50|11|5[0-9])\d{4}$"), "SH"),  # 沪股 + 沪 ETF/LOF
    (re.compile(r"^(00|30|15|16|159|12|18)\d{3,4}$"), "SZ"),  # 深股 + 深 ETF/LOF
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
    GEM = "gem"  # 创业板 300/301
    STAR = "star"  # 科创板 688
    BSE = "bse"  # 北交所
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
        # 沪市 000xxx 全段是指数（上证指数/上证A股指数/行业指数等）——
        # 沪市股票从 600 起，绝无 000 段；只硬编码 4 只会漏掉其余指数
        # （实测 000002.SH 上证A股指数等 63 只被误标成 stock）。
        if self.exchange == "SH" and c.startswith("000"):
            return SecType.INDEX
        if c.startswith("399") and self.exchange == "SZ":
            return SecType.INDEX
        return SecType.STOCK

    @property
    def board(self) -> Board:
        c = self.code
        # 689 是科创板存托凭证（CDR，如 689009.SH 九号公司），与 688 同档
        if c.startswith(("688", "689")):
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


def now_cn_naive() -> datetime:
    """Asia/Shanghai 墙钟时间（naive），用于写 DuckDB TIMESTAMP 列。

    DuckDB 的 TIMESTAMP 不带时区；直接写 tz-aware 值会带上偏移或被截断，
    各模块因此各自 ``now_cn().replace(tzinfo=None)``。集中在这里是为了让
    「lineage / updated_at 用哪个时区」只有一个答案 —— 此前 daily 用 CN
    墙钟、adj 用本机墙钟，服务器时区非 Asia/Shanghai 时同一批数据的
    ingested_at 会差 8 小时。
    """
    return now_cn().replace(tzinfo=None)


def today_cn() -> date:
    return now_cn().date()
