from lquant.data.schema import DAILY_BAR


def test_daily_bar_has_quant_columns():
    cols = ("pct_chg", "is_st", "is_suspended", "pe_ttm", "pb_mrq",
            "ps_ttm", "pcf_ncf_ttm", "total_mv", "float_mv")
    for col in cols:
        assert col in DAILY_BAR, col


def test_daily_bar_types():
    assert str(DAILY_BAR["pct_chg"]) == "Float64"
    assert str(DAILY_BAR["is_st"]) == "Boolean"
    assert str(DAILY_BAR["is_suspended"]) == "Boolean"
