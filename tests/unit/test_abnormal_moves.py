"""交易所异常波动偏离值（``lquant.data.quality.abnormal``）。

覆盖：分板块阈值取对、负向阈值严格于正向（规则本身，不许对称化）、
接近度分级边界（恰好 1.0 / 0.7 / 0.5）、指数缺日期时的对齐行为、
以及退化输入不崩。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.core.types import Board
from lquant.data.quality.abnormal import (
    STATUS_EDGE,
    STATUS_NORMAL,
    STATUS_TRIGGERED,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    THRESHOLDS,
    board_of,
    deviation_ratio,
    proximity,
    proximity_table,
    thresholds_for,
)

_PANEL = (
    Board.MAIN, Board.GEM, Board.STAR, Board.BSE,
)


def _series(closes: list[float], start: int = 1) -> pl.DataFrame:
    base = date(2026, 1, start)
    return pl.DataFrame({
        "trade_date": [base + timedelta(days=i) for i in range(len(closes))],
        "close": closes,
    })


# ---------- 分板块阈值 ----------


@pytest.mark.parametrize(("symbol", "board"), [
    ("600000.SH", Board.MAIN),
    ("000001.SZ", Board.MAIN),
    ("300750.SZ", Board.GEM),
    ("301001.SZ", Board.GEM),
    ("688981.SH", Board.STAR),
    ("689009.SH", Board.STAR),          # 科创板 CDR 与 688 同档
    ("830799.BJ", Board.BSE),
    ("510300.SH", Board.UNKNOWN),       # ETF 不适用本规则
])
def test_board_of_reuses_core_types(symbol, board):
    assert board_of(symbol) is board


def test_three_day_thresholds_by_board():
    assert thresholds_for(Board.MAIN, 3) == (0.20, 0.20)
    assert thresholds_for(Board.GEM, 3) == (0.30, 0.30)
    assert thresholds_for(Board.STAR, 3) == (0.30, 0.30)
    assert thresholds_for(Board.BSE, 3) == (0.40, 0.40)


def test_ten_and_thirty_day_thresholds_are_shared_across_boards():
    for board in _PANEL:
        assert thresholds_for(board, 10) == (1.00, 0.50)
        assert thresholds_for(board, 30) == (2.00, 0.70)


def test_negative_threshold_is_strictly_tighter_where_rule_says_so():
    """负向严于正向是规则本身。

    若有人"顺手对称化"（取两侧较宽或较严的绝对值），这条会红 —— 那正是它
    存在的意义：下跌方向的严重异常波动会因此被漏报。
    """
    for board, table in THRESHOLDS.items():
        for window, (up, down) in table.items():
            assert down <= up, f"{board} {window} 负向阈值不应比正向宽"
            if window in (10, 30):
                assert down < up, f"{board} {window} 负向阈值必须严格更严"
            else:
                assert down == up, f"{board} {window} 3 日档为对称口径"
    assert thresholds_for(Board.MAIN, 10) == (1.00, 0.50)
    assert thresholds_for(Board.MAIN, 30) == (2.00, 0.70)


def test_unknown_board_or_window_raises():
    with pytest.raises(ValueError, match="无异常波动阈值"):
        thresholds_for(Board.UNKNOWN, 3)
    with pytest.raises(ValueError, match="不在规则内"):
        thresholds_for(Board.MAIN, 5)


# ---------- 接近度分级边界 ----------


@pytest.mark.parametrize("board", _PANEL)
def test_closeness_boundaries_at_exactly_1_0_0_7_0_5(board):
    up, down = thresholds_for(board, 10)          # (1.00, 0.50)
    assert proximity(up, 10, board=board).status == STATUS_TRIGGERED
    assert proximity(up * 0.7, 10, board=board).status == STATUS_EDGE
    assert proximity(up * 0.5, 10, board=board).status == STATUS_WATCH
    assert proximity(up * 0.5 - 1e-9, 10, board=board).status == STATUS_NORMAL

    assert proximity(-down, 10, board=board).status == STATUS_TRIGGERED
    assert proximity(-down * 0.7, 10, board=board).status == STATUS_EDGE
    assert proximity(-down * 0.5, 10, board=board).status == STATUS_WATCH
    assert proximity(-down * 0.5 + 1e-9, 10, board=board).status == STATUS_NORMAL


def test_negative_side_uses_the_tighter_threshold():
    """同样的 |偏离|，在下跌方向更接近触发 —— 这就是不对称阈值的可观测后果。"""
    up_side = proximity(0.60, 10, board=Board.MAIN)
    down_side = proximity(-0.60, 10, board=Board.MAIN)
    assert up_side.threshold == 1.00 and up_side.status == STATUS_WATCH
    assert down_side.threshold == 0.50 and down_side.direction == "down"
    assert down_side.closeness == pytest.approx(1.2)
    assert down_side.status == STATUS_TRIGGERED


def test_gem_three_day_uses_30_percent():
    """创业板 3 日 ±30%：0.20 的偏离只是 watch，主板同值已是已触发。"""
    assert proximity(0.20, 3, symbol="300750.SZ").status == STATUS_WATCH
    assert proximity(0.20, 3, symbol="600000.SH").status == STATUS_TRIGGERED
    assert proximity(0.21, 3, symbol="830799.BJ").status == STATUS_WATCH


def test_proximity_keeps_raw_value_and_board():
    p = proximity(-0.35, 10, symbol="688981.SH")
    assert p.board is Board.STAR
    assert p.deviation == pytest.approx(-0.35)
    assert p.closeness == pytest.approx(0.7)
    assert p.window == 10
    assert p.status_label == "边缘"
    payload = p.to_dict()
    assert payload["board"] == "star" and payload["threshold"] == 0.50


def test_proximity_unknown_when_no_data():
    p = proximity(None, 3, symbol="600000.SH")
    assert p.status == STATUS_UNKNOWN and p.closeness is None and p.deviation is None
    assert proximity(float("nan"), 3, board=Board.MAIN).status == STATUS_UNKNOWN


def test_proximity_requires_a_board():
    with pytest.raises(ValueError, match="board="):
        proximity(0.1, 3)
    with pytest.raises(ValueError, match="无法从"):
        proximity(0.1, 3, symbol="510300.SH")     # ETF 无板块 → 不猜


# ---------- 偏离值计算与对齐 ----------


def test_deviation_ratio_numeric():
    stock = _series([10.0, 10.0, 10.0, 11.0, 12.5])
    index = _series([1000.0, 1000.0, 1000.0, 1010.0, 1020.0])
    out = deviation_ratio(stock, index, windows=(3,))
    assert out.columns == ["trade_date", "close", "close_idx", "deviate_3d"]
    # (12.5/10 - 1) - (1020/1000 - 1) = 0.25 - 0.02
    assert out["deviate_3d"][-1] == pytest.approx(0.23)
    assert out["deviate_3d"][:3].is_null().all()  # 历史不足 → null，不补 0


def test_multiple_windows_columns():
    stock = _series([10.0 + i for i in range(35)])
    index = _series([1000.0 + i for i in range(35)])
    out = deviation_ratio(stock, index)
    assert {"deviate_3d", "deviate_10d", "deviate_30d"} <= set(out.columns)
    assert out.height == 35
    assert out["deviate_30d"][-1] is not None and out["deviate_30d"][0] is None


def test_missing_index_date_is_dropped_not_filled():
    stock = _series([10.0, 10.2, 10.4, 10.6, 11.0])
    index = _series([1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
    index = index.filter(pl.col("trade_date") != date(2026, 1, 3))   # 指数缺一天
    out = deviation_ratio(stock, index, windows=(3,))
    assert out.height == 4
    assert date(2026, 1, 3) not in out["trade_date"].to_list()
    # 对齐后第 3 行 = 对齐序列的第 3 行（跨度实际是 4 个自然交易日）
    assert out["trade_date"].to_list() == [date(2026, 1, 1), date(2026, 1, 2),
                                           date(2026, 1, 4), date(2026, 1, 5)]
    # 末行回看的是对齐序列的首行（01-01），指数同期涨跌幅为 0
    assert out["deviate_3d"][-1] == pytest.approx(11.0 / 10.0 - 1.0)


def test_no_overlap_returns_empty_with_columns():
    stock = _series([10.0, 11.0])
    index = pl.DataFrame({"trade_date": [date(2026, 2, 1), date(2026, 2, 2)],
                          "close": [1000.0, 1010.0]})
    out = deviation_ratio(stock, index, windows=(3,))
    assert out.height == 0
    assert "deviate_3d" in out.columns


# ---------- 退化输入 ----------


def test_duplicate_dates_raise():
    dup = pl.DataFrame({"trade_date": [date(2026, 1, 1), date(2026, 1, 1)],
                        "close": [10.0, 11.0]})
    with pytest.raises(ValueError, match="重复"):
        deviation_ratio(dup, _series([1000.0, 1001.0]))


def test_missing_columns_raise_loudly():
    ok = _series([10.0, 11.0, 12.0])
    with pytest.raises(ValueError, match="stock_df 缺少必需列"):
        deviation_ratio(pl.DataFrame({"trade_date": [date(2026, 1, 1)]}), ok)
    with pytest.raises(ValueError, match="index_df 缺少必需列"):
        deviation_ratio(ok, pl.DataFrame({"trade_date": [date(2026, 1, 1)]}))


def test_empty_frames_do_not_crash():
    empty = pl.DataFrame(schema={"trade_date": pl.Date, "close": pl.Float64})
    out = deviation_ratio(empty, empty, windows=(3, 10))
    assert out.height == 0
    assert {"deviate_3d", "deviate_10d"} <= set(out.columns)


def test_short_frame_yields_null_deviation():
    out = deviation_ratio(_series([10.0]), _series([1000.0]), windows=(3,))
    assert out.height == 1 and out["deviate_3d"][0] is None


def test_bad_windows_raise():
    stock, index = _series([10.0] * 5), _series([1000.0] * 5)
    with pytest.raises(ValueError, match="不能为空"):
        deviation_ratio(stock, index, windows=())
    with pytest.raises(ValueError, match="正整数"):
        deviation_ratio(stock, index, windows=(0,))
    with pytest.raises(ValueError, match="正整数"):
        deviation_ratio(stock, index, windows=(3.5,))  # type: ignore[arg-type]


# ---------- 批量分级 ----------


def test_proximity_table_reads_last_row_and_skips_missing_windows():
    stock = _series([10.0 * (1.2 ** i) for i in range(15)])
    index = _series([1000.0] * 15)
    deviations = deviation_ratio(stock, index, windows=(3, 10, 30))
    table = proximity_table(deviations, symbol="600000.SH")
    by_window = {p.window: p for p in table}
    assert set(by_window) == {3, 10, 30}          # 30 日列存在但值为 null
    assert by_window[30].status == STATUS_UNKNOWN
    assert by_window[3].deviation == pytest.approx(1.2 ** 3 - 1.0)
    assert by_window[3].status == STATUS_TRIGGERED

    only_3 = deviations.select(["trade_date", "deviate_3d"])
    assert [p.window for p in proximity_table(only_3, symbol="600000.SH")] == [3]


def test_proximity_table_empty_frame_raises():
    empty = pl.DataFrame(schema={"trade_date": pl.Date, "deviate_3d": pl.Float64})
    with pytest.raises(ValueError, match="为空"):
        proximity_table(empty, symbol="600000.SH")
