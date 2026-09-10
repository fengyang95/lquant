"""link.py 单元测试:词表匹配 + 关键词映射 + 反查。"""

import polars as pl

from lquant.news.link import build_name_to_code, link_industry, link_symbols
from lquant.news.model import NewsItem


def _item(text):
    return NewsItem(
        source="news", source_name="em", external_id="1", title=text, content="", url="u"
    )


def test_link_symbols_multi_hit():
    n2c = {"平安银行": "000001.SZ", "宁德时代": "300750.SZ"}
    it = link_symbols(_item("宁德时代与平安银行合作"), n2c)
    assert set(it.symbols) == {"000001.SZ", "300750.SZ"}
    assert it.quality_flags & 4


def test_link_symbols_longest_first():
    n2c = {"银行": "999999.SZ", "平安银行": "000001.SZ"}
    it = link_symbols(_item("平安银行年报"), n2c)
    # "平安银行" 优先于前缀 "银行"
    assert it.symbols == ("000001.SZ",)


def test_link_no_hit_unchanged():
    it = link_symbols(_item("无命中文本"), {"平安银行": "000001.SZ"})
    assert it.symbols == ()
    assert it.quality_flags == 0


def test_link_industry_priority():
    kw = {"锂矿": "BK1"}
    s2i = {"300750.SZ": "BK2"}
    # kw 命中 → BK1;无 kw 命中但 symbols 含 300750.SZ → BK2;都无 → None
    it1 = link_industry(_item("锂矿扩产 宁德时代受益"), kw, s2i)
    assert it1.industry_code == "BK1"
    it2 = link_industry(_item("宁德时代发新品"), kw, s2i)  # 无 symbols 时反查不生效
    it3 = link_industry(_item("x"), kw, s2i)
    assert it2.industry_code is None and it3.industry_code is None
    it4 = NewsItem(
        source="news",
        source_name="em",
        external_id="1",
        title="x",
        content="",
        url="u",
        symbols=("300750.SZ",),
    )
    it5 = link_industry(it4, kw, s2i)
    assert it5.industry_code == "BK2"


def test_build_name_to_code():
    df = pl.DataFrame(
        {
            "name": ["平安银行", None, " 宁德时代 "],
            "symbol": ["000001.SZ", "X", "300750.SZ"],
        }
    )
    assert build_name_to_code(df) == {"平安银行": "000001.SZ", "宁德时代": "300750.SZ"}
