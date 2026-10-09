"""交易所异常波动 / 严重异常波动的**偏离值**与接近度（纯日线，零新依赖）。

规则口径与来源
--------------
偏离值定义::

    偏离值 = 个股 N 日累计涨跌幅 − 对应指数同期涨跌幅

阈值（小数制）来自 tick-stock-panel（MIT）``backend/app/services/abnormal_moves.py``
的模块 docstring 与 ``_MAIN`` / ``_GEM_STAR`` / ``_BSE`` 规则表，其注释指向
上交所《交易规则》5.4.2（3 日异常波动）/ 5.4.3（严重异常波动）、科创板
6.10 / 6.11 与北交所《交易规则》：

===========  ================  =============  =============
板块          3 日               10 日          30 日
===========  ================  =============  =============
主板          ±20%              +100% / −50%   +200% / −70%
创业板/科创板  ±30%              +100% / −50%   +200% / −70%
北交所        ±40%              +100% / −50%   +200% / −70%
===========  ================  =============  =============

**负向阈值显著严于正向（+100% vs −50%、+200% vs −70%）是规则本身**，不是笔误；
实现里绝不做「对称化」（取两侧较宽/较严的绝对值），否则会漏掉即将触发
严重异常波动的下跌标的 —— 而下跌触发才是风控真正关心的方向。

口径存疑（请以交易所现行规则为准）
----------------------------------
1. 表内数值转述自上述参考实现，本仓库**未**逐条比对交易所原文条款号；
   若规则修订，只需改 :data:`THRESHOLDS` 一处。
2. 「基本面/其他因素」豁免、以及「10 日内 4 次同向异常波动」（科创板 3 次）
   这类**事件计数**条款不在本模块范围 —— 它们需要跨窗口事件聚合，不是
   N 日累计涨跌幅能表达的。
3. ST/*ST：参考实现注明自 2026-07-06 起主板风险警示股票与普通股票同口径
   （原「3 日 ±15% / 10 日 +50% / 30 日 +100%」特别规定废止），因此这里
   **不为 ST 单独分档**，``st`` 只作展示标记。
4. 板块判定复用 :meth:`lquant.core.types.Symbol.board`（600/601/603/605 主板、
   300/301/302 创业板、688/689 科创板、BJ 北交所）；无法判定的代码
   （ETF/指数/债券等）**显式抛错**而不是猜一个板块 —— 阈值猜错等于给风控
   一个错误的警报线。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import polars as pl

from lquant.core.types import Board, parse_symbol

__all__ = [
    "STATUS_EDGE",
    "STATUS_NORMAL",
    "STATUS_TRIGGERED",
    "STATUS_UNKNOWN",
    "STATUS_WATCH",
    "THRESHOLDS",
    "WINDOWS",
    "Proximity",
    "board_of",
    "deviation_ratio",
    "proximity",
    "proximity_table",
    "thresholds_for",
]

#: 规则覆盖的窗口（交易日）。
WINDOWS: tuple[int, ...] = (3, 10, 30)

#: 板块 → {窗口: (正向阈值, 负向阈值**绝对值**)}，小数制。
#: 负向阈值不比正向大是**规则要求**（见模块 docstring），不要顺手改对称。
THRESHOLDS: dict[Board, dict[int, tuple[float, float]]] = {
    Board.MAIN: {3: (0.20, 0.20), 10: (1.00, 0.50), 30: (2.00, 0.70)},
    Board.GEM: {3: (0.30, 0.30), 10: (1.00, 0.50), 30: (2.00, 0.70)},
    Board.STAR: {3: (0.30, 0.30), 10: (1.00, 0.50), 30: (2.00, 0.70)},
    Board.BSE: {3: (0.40, 0.40), 10: (1.00, 0.50), 30: (2.00, 0.70)},
}

#: 接近度分级：≤ 阈值这个比例就进入对应档位。
STATUS_TRIGGERED = "triggered"   # ≥ 1.0 已触发
STATUS_EDGE = "edge"             # ≥ 0.7 边缘
STATUS_WATCH = "watch"           # ≥ 0.5 观察
STATUS_NORMAL = "normal"         # < 0.5 无信号
STATUS_UNKNOWN = "unknown"       # 偏离值缺数据，无法判级

#: 分级中文名，给前端/通知直接用。
STATUS_LABELS = {
    STATUS_TRIGGERED: "已触发",
    STATUS_EDGE: "边缘",
    STATUS_WATCH: "观察",
    STATUS_NORMAL: "正常",
    STATUS_UNKNOWN: "数据不足",
}


def _finite(v: Any) -> float | None:
    """转成有限 float；null/NaN/±Inf/非数值一律 None（不抛）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def board_of(symbol: str) -> Board:
    """标的 → 板块，复用 :class:`lquant.core.types.Symbol` 的既有口径。

    不自己写一套代码段匹配：板块口径在 ``core/types.py`` 已经统一
    （含 689 CDR、302 创业板新段），重复实现必然分叉。
    """
    try:
        return parse_symbol(str(symbol)).board
    except (ValueError, AttributeError):
        return Board.UNKNOWN


def _require_board(board: Board | None, symbol: str | None) -> Board:
    """确定板块：显式 ``board`` 优先，否则从 ``symbol`` 推；不可判定即抛错。"""
    if board is not None and board is not Board.UNKNOWN:
        return board
    if symbol:
        inferred = board_of(symbol)
        if inferred is not Board.UNKNOWN:
            return inferred
        raise ValueError(f"无法从 {symbol!r} 判定板块，阈值无法选取；请显式传 board=")
    raise ValueError("必须提供 board= 或 symbol= —— 阈值按板块分档，缺一不可")


def thresholds_for(board: Board, window: int) -> tuple[float, float]:
    """取 (正向阈值, 负向阈值绝对值)。

    Raises:
        ValueError: 板块不可判定（``Board.UNKNOWN``）或窗口不在规则内。
    """
    if board not in THRESHOLDS:
        raise ValueError(
            f"板块 {board!r} 无异常波动阈值（可选: {[b.value for b in THRESHOLDS]}）；"
            "ETF/指数/债券不适用本规则"
        )
    table = THRESHOLDS[board]
    if window not in table:
        raise ValueError(f"窗口 {window} 不在规则内，可选: {sorted(table)}")
    return table[window]


def _check_windows(windows: tuple[int, ...]) -> tuple[int, ...]:
    if not windows:
        raise ValueError("windows 不能为空")
    bad = [w for w in windows
           if not isinstance(w, int) or isinstance(w, bool) or w < 1]
    if bad:
        raise ValueError(f"window 必须是正整数（交易日数），得到 {bad}")
    return tuple(windows)


def _check_price_frame(df: pl.DataFrame, *, name: str, price_col: str,
                       date_col: str) -> None:
    missing = [c for c in (date_col, price_col) if c not in df.columns]
    if missing:
        raise ValueError(f"{name} 缺少必需列 {missing}；现有列 {list(df.columns)}")


def _clean_price(df: pl.DataFrame, *, name: str, price_col: str,
                 date_col: str) -> pl.DataFrame:
    """单标的价格序列：dropnull → 排序 → 去重（重复交易日直接抛错）。"""
    _check_price_frame(df, name=name, price_col=price_col, date_col=date_col)
    out = df.select([date_col, price_col]).drop_nulls()
    dup = out.height - out.unique(subset=[date_col]).height
    if dup:
        raise ValueError(
            f"{name} 存在 {dup} 个重复 {date_col}：同一天两条价格无法确定累计涨跌幅"
            "该怎么算，请先聚合"
        )
    return out.sort(date_col)


def deviation_ratio(
    stock_df: pl.DataFrame,
    index_df: pl.DataFrame,
    windows: tuple[int, ...] = WINDOWS,
    *,
    price_col: str = "close",
    date_col: str = "trade_date",
) -> pl.DataFrame:
    """N 日累计涨跌幅偏离值（个股 − 指数），按交易日对齐、**缺日不补**。

    Args:
        stock_df: 单标的日线，需含 ``date_col`` / ``price_col``。
        index_df: 对应基准指数日线（同一 ``date_col``）。
        windows: 窗口（交易日数），默认 ``(3, 10, 30)``。
        price_col / date_col: 价格列 / 日期列名。

    Returns:
        列 = ``date_col`` + ``deviate_{n}d``（n 为每个窗口）。前 n 行天然为 null
        （历史不足），**保留**而不是丢掉 —— 调用方能看到对齐后的完整序列。

    对齐口径（**为什么先 join 再算收益**）：
        两个序列先按 ``date_col`` 做 inner join，再在**对齐后的序列**上算
        float("N 日") 累计涨跌幅。指数缺某一天时，该日直接不出现（缺日不补、
        不 forward-fill）—— 前复权/停牌造成的指数缺口用最近值填过去，会让
        "同期指数涨跌幅"失真，进而把偏离值算错方向。
        代价是：join 后的第 N 行覆盖的日历跨度可能 > N 个自然交易日，
        交易所口径的"N 个交易日"因此是**双方都有报价的 N 个交易日**。

    Raises:
        ValueError: 缺列 / 空窗口 / 非法窗口 / 重复交易日。
    """
    checked = _check_windows(tuple(windows))
    stock = _clean_price(stock_df, name="stock_df", price_col=price_col,
                         date_col=date_col)
    index = _clean_price(index_df, name="index_df", price_col=price_col,
                         date_col=date_col)
    joined = stock.join(index, on=date_col, how="inner", suffix="_idx")
    exprs = [
        (
            (pl.col(price_col) / pl.col(price_col).shift(n) - 1.0)
            - (pl.col(f"{price_col}_idx") / pl.col(f"{price_col}_idx").shift(n) - 1.0)
        ).alias(f"deviate_{n}d")
        for n in checked
    ]
    if not exprs:
        return joined
    return joined.with_columns(exprs)


@dataclass(frozen=True)
class Proximity:
    """单个窗口的接近度判定结果。

    Attributes:
        window: 窗口（交易日数）。
        board: 所用的板块 —— 阈值就是按它取的，必须随结果一起保留。
        deviation: 原始偏离值（小数制）；数据不足为 ``None``。
        threshold: 本次实际使用的阈值（方向对应的那一个，正数）。
        closeness: ``|偏离值| / 阈值``；数据不足为 ``None``。
        direction: ``up`` / ``down`` / ``flat``，负向阈值更严体现在这里。
        status: 见 :data:`STATUS_TRIGGERED` 等常量。
    """

    window: int
    board: Board
    deviation: float | None
    threshold: float
    closeness: float | None
    direction: str
    status: str

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "board": self.board.value,
            "deviation": self.deviation,
            "threshold": self.threshold,
            "closeness": self.closeness,
            "direction": self.direction,
            "status": self.status,
            "status_label": self.status_label,
        }


def proximity(
    deviation: float | None,
    window: int,
    *,
    symbol: str | None = None,
    board: Board | None = None,
) -> Proximity:
    """单窗口接近度分级。

        接近度 = |偏离值| / **该方向**阈值
        ≥ 1.0 已触发 / ≥ 0.7 边缘 / ≥ 0.5 观察 / < 0.5 正常

    分母取方向对应的阈值：正偏离用正向阈值，负偏离用**更严**的负向阈值。
    若统一取两者较大的那个，下跌方向的接近度会被系统性低估，风控就看不到
    「−50% 已经打满」这种状态。

    Args:
        deviation: 偏离值（小数制，如 0.21 表示 +21%）；``None`` → 判 ``unknown``。
        window: 窗口（交易日数），须在规则表内。
        symbol / board: 二者至少给一个；都给时以 ``board`` 为准。
    """
    resolved = _require_board(board, symbol)
    up, down = thresholds_for(resolved, window)
    # NaN/±Inf 一律当「数据不足」：让它落进 normal 会把脏数据伪装成「无异动」。
    dev = _finite(deviation)
    if dev is None:
        return Proximity(window=window, board=resolved, deviation=None,
                         threshold=up, closeness=None, direction="flat",
                         status=STATUS_UNKNOWN)
    if dev > 0:
        direction, threshold = "up", up
    elif dev < 0:
        direction, threshold = "down", down
    else:
        direction, threshold = "flat", up
    closeness = abs(dev) / threshold
    if closeness >= 1.0:
        status = STATUS_TRIGGERED
    elif closeness >= 0.7:
        status = STATUS_EDGE
    elif closeness >= 0.5:
        status = STATUS_WATCH
    else:
        status = STATUS_NORMAL
    return Proximity(window=window, board=resolved, deviation=dev,
                     threshold=threshold, closeness=closeness,
                     direction=direction, status=status)


def proximity_table(
    deviations: pl.DataFrame,
    *,
    symbol: str | None = None,
    board: Board | None = None,
    windows: tuple[int, ...] = WINDOWS,
    row: int = -1,
) -> list[Proximity]:
    """对 :func:`deviation_ratio` 的结果按行（默认最后一行）批量分级。

    缺某窗口的 ``deviate_{n}d`` 列时跳过该窗口（而不是当 0 处理 —— 0 偏离
    会被判成「正常」，把「没数据」和「没异动」混为一谈）。
    """
    checked = _check_windows(tuple(windows))
    if deviations.height == 0:
        raise ValueError("deviations 为空，无法判定接近度")
    out: list[Proximity] = []
    for n in checked:
        col = f"deviate_{n}d"
        if col not in deviations.columns:
            continue
        value = deviations[col][row]
        out.append(proximity(None if value is None else float(value), n,
                             symbol=symbol, board=board))
    return out
