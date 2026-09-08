from datetime import date

from lquant.backtest.rules.loader import load_ruleset
from lquant.core.types import Board, SecType


def test_etf_no_stamp_tax():
    rs = load_ruleset()
    r = rs.for_symbol("510300.SH", SecType.ETF, Board.MAIN)
    assert r.tax_rate(date(2026, 1, 1)) == 0.0


def test_tax_schedule_historical():
    # 2023-08-28 起印花税从千一降到万五
    rs = load_ruleset()
    r = rs.for_symbol("600000.SH", SecType.STOCK, Board.MAIN)
    assert r.tax_rate(date(2023, 1, 1)) == 0.001
    assert r.tax_rate(date(2024, 1, 1)) == 0.0005


def test_t_plus_per_instrument():
    # QDII ETF 是 T+0，per-instrument 覆盖优先于类型默认值
    rs = load_ruleset()
    r = rs.for_symbol("513050.SH", SecType.ETF, Board.MAIN, fund_type="qdii",
                      sellable_after_days=0)
    assert r.sellable_after_days == 0
