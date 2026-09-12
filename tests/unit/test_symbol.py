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


def test_sh_000_segment_is_index():
    """回归：沪市 000xxx 全段是指数，只硬编码 4 只会漏。

    实测 000002.SH（上证A股指数）、000006.SH（上证房地产指数）等
    63 只被误标成 stock → 混进日线回填池 → 量纲/价格护栏 fatal。
    """
    for code in ("000001", "000002", "000006", "000300", "000688", "000905"):
        assert parse_symbol(f"{code}.SH").sec_type == SecType.INDEX, code
    # 深市 000 段仍是股票
    assert parse_symbol("000002.SZ").sec_type == SecType.STOCK
    assert parse_symbol("000001.SZ").sec_type == SecType.STOCK
