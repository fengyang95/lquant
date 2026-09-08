from lquant.core.types import SecType, parse_symbol


def test_parse_variants():
    assert str(parse_symbol("sh.600000")) == "600000.SH"
    assert str(parse_symbol("600000")) == "600000.SH"
    assert str(parse_symbol("600519.XSHG")) == "600519.SH"


def test_ambiguous_code():
    # 000001 是歧义码，必须带交易所
    assert parse_symbol("000001.SZ").sec_type == SecType.STOCK


def test_etf():
    assert parse_symbol("510300").exchange == "SH"
