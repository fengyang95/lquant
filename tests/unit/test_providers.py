"""Phase 2 Provider 生态的离线单测。

parse_* 纯函数（网络分离）直接测；含跨源对拍（§3.8.4）的纯函数层。
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest


def _gtimg_payload(fields: list[str]) -> str:
    """按下标精确构造 qt.gtimg.cn 快照文本（避免手数 ~ 下标出错）。"""
    return 'v_sh600519="' + "~".join(fields) + '~";'


# ---------------- tencent ----------------
class TestTencentParse:
    def test_parse_quote_single(self):
        from lquant.data.providers.tencent import parse_quotes

        f = [str(i) for i in range(50)]       # 占位，仅保长度
        f[1] = "贵州茅台"
        f[3] = "1700.00"
        f[4] = "1690.00"
        f[5] = "1695.00"
        f[6] = "12345"
        f[30] = "20240909150003"
        f[32] = "-1.20"
        f[33] = "1720.00"
        f[34] = "1680.00"
        f[37] = "586134.00"
        f[38] = "0.05"
        f[47] = "1747.00"
        f[48] = "1650.00"
        df = parse_quotes(_gtimg_payload(f))
        assert len(df) == 1
        r = df.row(0, named=True)
        assert r["symbol"] == "600519.SH"
        assert r["name"] == "贵州茅台"
        assert r["last"] == 1700.0
        assert r["pre_close"] == 1690.0
        assert r["open"] == 1695.0
        assert r["volume"] == 12345 * 100.0    # 手 → 股（index 6）
        assert r["amount"] == 586134.0 * 1e4   # 万元 → 元
        assert r["pct_chg"] == -1.20
        assert r["turnover_rate"] == 0.05
        assert r["source"] == "tencent"
        assert r["ts"].strftime("%Y%m%d%H%M%S") == "20240909150003"

    def test_parse_multi_and_skip_empty(self):
        from lquant.data.providers.tencent import parse_quotes

        payload = 'v_sh600000="1~浦发银行~600000~7.25~";\nv_sz000001="1~平安银行~1";'
        df = parse_quotes(payload)
        # 第二行 body 极短（只有价格前字段）→ 跳过；第一行 price 有值
        assert len(df) == 1

    def test_parse_no_quotes(self):
        from lquant.data.providers.tencent import parse_quotes

        assert len(parse_quotes('v_sh=;')) == 0
        assert len(parse_quotes("")) == 0

    def test_gtimg_code(self):
        from lquant.data.providers.tencent import _gtimg_code

        assert _gtimg_code("600519.SH") == "sh600519"
        assert _gtimg_code("000001.SZ") == "sz000001"
        assert _gtimg_code("830799.BJ") == "bj830799"

    def test_provider_capability_and_nocap_raise(self):
        from lquant.core.errors import CapabilityMissing
        from lquant.data.capability import Capability
        from lquant.data.providers.tencent import TencentProvider

        p = TencentProvider()
        assert p.has(Capability.REALTIME)
        assert not p.has(Capability.DAILY)
        with pytest.raises(CapabilityMissing):
            p.daily_bars([], None, None)


# ---------------- sina ----------------
class TestSinaParse:
    def test_parse_hfq_data_key(self):
        from lquant.data.providers.sina import parse_hfq

        payload = ('var _sh600519_hfq={"data":'
                   '[["2024-09-09",1700.0,1720,1700,1680,12345],'
                   '["2024-09-06",1680.0,1680,1650,1640,10000]]}')
        dates, closes = parse_hfq(payload)
        assert dates == ["2024-09-09", "2024-09-06"]
        assert closes == [1680.0, 1640.0]    # hfq 行下标 4 = close

    def test_parse_hfq_year_grouped(self):
        from lquant.data.providers.sina import parse_hfq

        payload = ('var _s={2024:[["2024-09-09",1700,1720,1700,1680,1]],'
                   '2023:[["2023-08-01",1500,1510,1490,1500,2]]}')
        dates, closes = parse_hfq(payload)
        assert set(dates) == {"2023-08-01", "2024-09-09"}
        assert set(closes) == {1500.0, 1680.0}

    def test_parse_hfq_schema_change_raises(self):
        from lquant.core.errors import SourceSchemaChanged
        from lquant.data.providers.sina import parse_hfq

        with pytest.raises(SourceSchemaChanged):
            parse_hfq('var x={"foo":42}')
        with pytest.raises(SourceSchemaChanged):
            parse_hfq("not json at all")

    def test_sina_code(self):
        from lquant.data.providers.sina import _sina_code

        assert _sina_code("600519.SH") == "sh600519"
        assert _sina_code("000001.SZ") == "sz000001"


# ---------------- mootdx ----------------
class TestMootdxParse:
    def test_parse_quotes_list(self):
        from lquant.market.providers.mootdx import parse_quotes

        records = [{
            "market": 1, "code": "600519", "price": 1700.0,
            "last_close": 1690.0, "open": 1695.0, "high": 1720.0, "low": 1680.0,
            "vol": 3456700, "amount": 5.8e9,
        }]
        df = parse_quotes(records)
        assert len(df) == 1
        r = df.row(0, named=True)
        assert r["symbol"] == "600519.SH"
        assert r["last"] == 1700.0
        assert r["open"] == 1695.0
        assert r["source"] == "mootdx"

    def test_parse_quotes_dict_wrapped(self):
        from lquant.market.providers.mootdx import parse_quotes

        df = parse_quotes({"data": [{"market": 0, "code": "000001",
                                     "price": 11.2, "last_close": 11.0}]})
        assert len(df) == 1
        assert df.row(0, named=True)["symbol"] == "000001.SZ"

    def test_parse_skips_zero_price(self):
        from lquant.market.providers.mootdx import parse_quotes

        df = parse_quotes([{"market": 1, "code": "600000", "price": 0}])
        assert len(df) == 0

    def test_parse_bad_market_record_does_not_crash(self):
        """market=None 的坏记录不拖垮整批：缺省按沪市推断（600 前缀），其余行照常。"""
        from lquant.market.providers.mootdx import parse_quotes

        records = [
            {"market": None, "code": "600519", "price": 1700.0},
            {"market": "x", "code": "000001", "price": 11.2},
            {"market": 1, "code": "600000", "price": 7.25},
            {"market": 0, "code": "000002", "price": 8.8},
        ]
        df = parse_quotes(records)
        assert len(df) == 4                      # 无一条导致整批崩溃
        syms = set(df["symbol"].to_list())
        assert "600519.SH" in syms               # market=None → 缺省 SH
        assert "000002.SZ" in syms               # market=0 → SZ 保留


# ---------------- crosscheck (§3.8.4) ----------------
class TestCrosscheck:
    def _daily(self, rows):
        cols = ["symbol", "trade_date", "open", "close"]
        return pl.DataFrame(rows, schema=cols)

    def test_classify_match_l1(self):
        from lquant.data.quality.crosscheck import classify_divergence

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        q = p
        d = classify_divergence(p, q, tolerance_pct=0.1, fields=("open", "close"))
        # 全部一致 → 每字段 L1（缺 peer/primary 视为一致的一部分）
        assert len(d) == 2
        assert set(d["level"].to_list()) == {"L1"}

    def test_classify_material_l2(self):
        from lquant.data.quality.crosscheck import classify_divergence

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        q = self._daily([["600519.SH", "2024-09-09", 1780.0, 1780.0]])  # +4.7%
        d = classify_divergence(p, q, tolerance_pct=0.1, fields=("open", "close"))
        assert set(d["level"].to_list()) == {"L2"}

    def test_classify_outlier_l3(self):
        from lquant.data.quality.crosscheck import classify_divergence

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        q = self._daily([["600519.SH", "2024-09-09", 3000.0, 3000.0]])  # 数量级差
        d = classify_divergence(p, q, tolerance_pct=0.1, fields=("open", "close"))
        assert set(d["level"].to_list()) == {"L3"}

    def test_classify_missing_peer(self):
        from lquant.data.quality.crosscheck import classify_divergence

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        q = pl.DataFrame(schema=p.schema)
        d = classify_divergence(p, q, fields=("open", "close"))
        # peer 全缺 → primary 有、peer 无 → missing=peer，逐字段 L1
        assert len(d) == 2
        assert d.filter(pl.col("missing") == "peer").height == 2

    def test_flag_cross_source_sets_bit(self):
        from lquant.data.quality.crosscheck import flag_cross_source
        from lquant.data.quality.flags import CROSS_SOURCE_DIFF

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        p = p.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
        diffs = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": ["2024-09-09"],
            "field": ["open"], "level": ["L2"],
        })
        out = flag_cross_source(p, diffs)
        assert (out.filter((pl.col("quality_flags") & CROSS_SOURCE_DIFF) != 0)
                .height == 1)
        # 数值不被改变（immutable）
        assert out.row(0, named=True)["open"] == 1700.0

    def test_flag_leaves_l1_alone(self):
        from lquant.data.quality.crosscheck import flag_cross_source
        from lquant.data.quality.flags import CROSS_SOURCE_DIFF

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        p = p.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
        diffs = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": ["2024-09-09"],
            "field": ["open"], "level": ["L1"],
        })
        out = flag_cross_source(p, diffs)
        assert (out.filter((pl.col("quality_flags") & CROSS_SOURCE_DIFF) != 0)
                .height == 0)

    def test_summarize(self):
        from lquant.data.quality.crosscheck import summarize

        d = pl.DataFrame({"level": ["L1", "L1", "L2", "L3"]})
        s = summarize(d)
        assert s["checked"] == 4
        assert s["L2"] == 1 and s["L3"] == 1

    def test_peer_only_row_keeps_identity_and_l3(self):
        """peer 独有日：full join coalesce 后 key 不丢，且判 L3 降级候选。"""
        from lquant.data.quality.crosscheck import classify_divergence

        p = self._daily([["600519.SH", "2024-09-09", 1700.0, 1700.0]])
        q = self._daily([
            ["600519.SH", "2024-09-09", 1700.0, 1700.0],
            ["600519.SH", "2024-09-11", 3400.0, 3400.0],   # primary 无此日
        ])
        d = classify_divergence(p, q, fields=("open", "close"))
        peer_only = d.filter(pl.col("missing") == "primary")
        # peer-only 行的 symbol/date 保留（coalesce 回归）；trade_date 已归一为 Date
        assert set(peer_only["symbol"].unique().to_list()) == {"600519.SH"}
        assert set(peer_only["trade_date"].unique().to_list()) == {dt.date(2024, 9, 11)}
        # 整行仅 peer 存在 → L3 降级候选；共同日仍 L1
        assert set(peer_only["level"].to_list()) == {"L3"}
        assert set(d.filter(pl.col("trade_date") == dt.date(2024, 9, 9))["level"].to_list()) == {"L1"}

    def test_partial_field_missing_is_l1_not_l3(self):
        """行不缺失（两侧都在），仅某字段单侧缺失 → L1，不是 L3。"""
        from lquant.data.quality.crosscheck import classify_divergence

        # 用 String 日期测 dtype 归一不影响
        p = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": ["2024-09-09"],
            "open": [1700.0], "close": [1700.0], "volume": [1000000.0]})
        q = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": ["2024-09-09"],
            "open": [1700.0], "close": [1700.0], "volume": [None]})
        d = classify_divergence(p, q, fields=("open", "close", "volume"))
        vol = d.filter(pl.col("field") == "volume")
        assert set(vol["level"].to_list()) == {"L1"}   # 单字段缺失，非降级
        assert set(vol["missing"].to_list()) == {"peer"}

    def test_date_dtype_normalized_before_join(self):
        """湖 Date 与 provider String/Datetime 混用不该抛 SchemaError。"""
        import datetime as dt

        from lquant.data.quality.crosscheck import classify_divergence
        p = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": [dt.date(2024, 9, 9)],
            "open": [1700.0]})
        q = pl.DataFrame({
            "symbol": ["600519.SH"], "trade_date": [dt.datetime(2024, 9, 9)],
            "open": [1700.0]})
        d = classify_divergence(p, q, fields=("open",))   # 不抛错
        assert len(d) == 1
        assert set(d["level"].to_list()) == {"L1"}