"""sector 采集器覆盖补齐：解析归一 + em_get 打桩的真实通路与 demo 路径。"""

from __future__ import annotations

from datetime import date

from lquant.core.types import today_cn
from lquant.market.collectors import sector as sec


def test_norm_parses_valid_symbol() -> None:
    assert sec._norm("600519") == "600519.SH"


def test_norm_keeps_invalid_code() -> None:
    assert sec._norm("BK0451") == "BK0451"


def test_as_date_variants() -> None:
    # 缺省业务日与生产侧同源（core.types.today_cn）。不能用 datetime.now()：
    # CI 跑在 UTC，UTC 16:00 之后就是 CN 的次日，断言会假红。
    assert sec._as_date(None) == today_cn()
    assert sec._as_date("2026-09-17") == date(2026, 9, 17)
    assert sec._as_date("20260917") == date(2026, 9, 17)
    assert sec._as_date(date(2026, 9, 17)) == date(2026, 9, 17)


def test_num_defaults() -> None:
    assert sec._num(None) == 0.0
    assert sec._num("") == 0.0
    assert sec._num("-") == 0.0
    assert sec._num("x") == 0.0
    assert sec._num(None, default=9.0) == 9.0
    assert sec._num("3.5") == 3.5


_DIFF = {
    "data": {"diff": [
        {"f12": "BK0451", "f14": "半导体", "f3": 3.2, "f8": 2.5,
         "f62": 1.2e9, "f207": "600519", "f128": "贵州茅台", "f136": 4.1,
         "f104": 30, "f105": 5},
        {"f12": "BK0452", "f14": "证券", "f3": "-", "f8": None,
         "f62": "", "f207": None, "f128": None, "f136": None,
         "f104": "10", "f105": "20"},
    ]},
}


def test_fetch_sectors_via_em_get(monkeypatch) -> None:
    captured = {}

    class _Resp:
        def json(self):
            return _DIFF

    def fake_em_get(url):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(sec, "em_get", fake_em_get)
    df = sec.fetch_sectors(date(2026, 9, 17), "industry")
    assert "m:90+t:2" in captured["url"]
    assert df["sector_code"].to_list() == ["BK0451", "BK0452"]
    top = df.row(0, named=True)
    assert top["leader_symbol"] == "600519.SH"      # _norm 归一
    assert top["up_count"] == 30
    second = df.row(1, named=True)
    assert second["change_pct"] == 0.0              # "-" → default 0
    assert second["leader_symbol"] is None          # f207 缺失 → None


def test_fetch_sectors_diff_dict_form(monkeypatch) -> None:
    diff = {"0": _DIFF["data"]["diff"][0]}

    class _Resp:
        def json(self):
            return {"data": {"diff": diff}}

    monkeypatch.setattr(sec, "em_get", lambda url: _Resp())
    df = sec.fetch_sectors("20260917", "concept")
    assert len(df) == 1
    assert df["kind"][0] == "concept"


def test_fetch_sectors_empty_diff_returns_empty_schema(monkeypatch) -> None:
    class _Resp:
        def json(self):
            return {"data": None}

    monkeypatch.setattr(sec, "em_get", lambda url: _Resp())
    df = sec.fetch_sectors()
    assert df.is_empty()
    assert df.columns == sec._empty().columns


def test_fetch_concepts_and_areas_routing(monkeypatch) -> None:
    urls = []

    class _Resp:
        def json(self):
            return {"data": {"diff": []}}

    def fake(url):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(sec, "em_get", fake)
    sec.fetch_concepts()
    sec.fetch_areas()
    assert "m:90+t:3" in urls[0]
    assert "m:90+t:1" in urls[1]


def test_demo_sectors_deterministic() -> None:
    d = date(2026, 9, 17)
    df1 = sec.fetch_sectors(d, "industry", demo=True)
    df2 = sec.fetch_sectors(d, "industry", demo=True)
    assert df1["change_pct"].to_list() == df2["change_pct"].to_list()
    assert df1["sector_code"][0].startswith("BK1")
    con = sec.fetch_sectors(d, "concept", demo=True)
    area = sec.fetch_sectors(d, "area", demo=True)
    assert con["sector_code"][0].startswith("BK2")
    assert area["sector_code"][0].startswith("BK3")
