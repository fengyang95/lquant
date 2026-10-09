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


def test_transfer_fee_schedule_historical():
    """过户费按监管区间取：2015-08-01 起 0.02‰，2022-04-29 起下调 50% 到 0.01‰。

    原来规则表只有单一常数 0.00001（现行费率），2022-04-29 之前的回测因此
    把过户费少算一半 —— 静默偏差。这条用例把这个口径钉住。
    """
    rs = load_ruleset()
    r = rs.for_symbol("600000.SH", SecType.STOCK, Board.MAIN)
    assert r.transfer_fee_rate_on(date(2022, 4, 28)) == 0.00002
    assert r.transfer_fee_rate_on(date(2022, 4, 29)) == 0.00001
    assert r.transfer_fee_rate_on(date(2026, 1, 5)) == 0.00001
    # 无日期上下文的路径（建仓资金预估）用现行常数，且必须与末段一致
    assert r.transfer_fee_rate == 0.00001


def test_transfer_fee_before_2015_fails_loud():
    """2015-08-01 之前过户费按成交面额计，模型表达不了 → 查不到区间必须抛错。

    宁可让长回测显式失败，也不要静默套用现行费率给出错的成本数字。
    """
    import pytest

    from lquant.core.errors import RuleNotFound

    rs = load_ruleset()
    r = rs.for_symbol("600000.SH", SecType.STOCK, Board.MAIN)
    with pytest.raises(RuleNotFound, match="过户费区间"):
        r.transfer_fee_rate_on(date(2014, 1, 2))


def test_transfer_fee_etf_is_zero_every_day():
    """ETF 无过户费：没有区间表，逐日路径退化为常数 0。"""
    rs = load_ruleset()
    r = rs.for_symbol("510300.SH", SecType.ETF, Board.MAIN)
    assert r.transfer_fee_rate_on(date(2018, 6, 1)) == 0.0
    assert r.transfer_fee_rate_on(date(2026, 1, 5)) == 0.0


def test_transfer_fee_flat_rate_mismatch_rejected():
    """常数费率与区间表末段不一致 → 加载即报错，防止两条路径悄悄分叉。"""
    import pytest

    from lquant.backtest.rules.model import RuleSet

    rs = RuleSet(
        market="CN", currency="CNY", etf={}, exceptions={},
        default={"transfer_fee": {
            "rate": 0.00002,
            "schedule": [{"from": "2022-04-29", "to": "9999-12-31",
                          "rate": 0.00001}],
        }},
    )
    with pytest.raises(ValueError, match="不一致"):
        rs.for_symbol("600000.SH", SecType.STOCK, Board.MAIN)
