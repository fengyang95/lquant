"""行业分析 API 契约测试（/api/industry/*）。

用例只测 HTTP 边界与参数归一，不碰真实数据湖：``analyze_industry`` 等被打桩，
因为「分析内容对不对」由 ``test_industry_analysis.py`` 覆盖，这里关心的是
路径参数透传、日期校验、错误码和响应形状。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_industry")
    prev_cwd = os.getcwd()  # 模块级 fixture 必须还原 CWD，否则污染后续测试文件
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def captured(monkeypatch):
    """打桩 analyze_industry，记录收到的 (identifier, asof, std)。"""
    import lquant.industry as ind

    seen: list[tuple[str, object, object]] = []

    def fake(identifier, asof=None, std=None):
        seen.append((identifier, asof, std))
        return {"industry": "银行", "industry_code": "801780.SI",
                "asof": "2026-09-30", "score": {"score": 60.0, "grade": "强势"},
                "angles": [], "verdict": {"points": [], "risks": []},
                "overview": {}, "risk": {}, "schema_version": "1.0"}

    monkeypatch.setattr(ind, "analyze_industry", fake)
    return seen


# ---------------------------------------------------------------- 角度注册表


def test_angles_registry(client):
    r = client.get("/api/industry/angles")
    assert r.status_code == 200
    body = r.json()
    ids = [a["id"] for a in body["angles"]]
    assert ids == ["trend", "prosperity", "valuation", "capital", "breadth"]
    total = sum(a["weight"] for a in body["angles"] if a["weight"] > 0)
    assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------- 分析端点


def test_code_identifier_is_passed_through(client, captured):
    r = client.get("/api/industry/801780.SI/analysis")
    assert r.status_code == 200
    assert captured[-1][0] == "801780.SI"
    assert captured[-1][1] is None


def test_chinese_name_identifier_is_passed_through(client, captured):
    """中文行业名是最常见的输入 —— 不能在 API 边界被改写。"""
    r = client.get("/api/industry/银行/analysis")
    assert r.status_code == 200
    assert captured[-1][0] == "银行"


def test_asof_and_std_are_parsed_and_forwarded(client, captured):
    r = client.get("/api/industry/银行/analysis?asof=2026-09-30&std=CICS")
    assert r.status_code == 200
    identifier, asof, std = captured[-1]
    assert identifier == "银行"
    assert asof is not None and asof.isoformat() == "2026-09-30"
    assert std == "CICS"


def test_invalid_asof_is_422(client, captured):
    r = client.get("/api/industry/银行/analysis?asof=2026/09/30")
    assert r.status_code == 422
    assert not captured


def test_unknown_industry_is_404(client, monkeypatch):
    import lquant.industry as ind

    def boom(identifier, asof=None, std=None):
        raise KeyError(identifier)

    monkeypatch.setattr(ind, "analyze_industry", boom)
    r = client.get("/api/industry/不存在行业/analysis")
    assert r.status_code == 404
    assert "不存在行业" in r.json()["detail"]


def test_404_detail_is_actionable_when_lake_empty(client, monkeypatch):
    """湖里没有行业分类时，404 必须说清楚「先同步什么」而不是只报找不到。"""
    import lquant.industry as ind

    def boom(identifier, asof=None, std=None):
        raise KeyError(identifier)

    monkeypatch.setattr(ind, "analyze_industry", boom)
    r = client.get("/api/industry/银行/analysis")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert ("industry_classify" in detail) or ("可用行业" in detail)


# ---------------------------------------------------------------- 清单与轮动


def test_list_endpoint_shape(client, monkeypatch):
    import lquant.industry as ind

    monkeypatch.setattr(ind, "list_industry_names", lambda asof=None, std=None: {
        "asof": "2026-09-30", "std": "SW",
        "industries": [{"industry_code": "801780.SI", "industry_name": "银行",
                        "n_members": 42, "last_ret": 0.012}],
        "notes": []})
    r = client.get("/api/industry/list")
    assert r.status_code == 200
    body = r.json()
    assert body["industries"][0]["industry_name"] == "银行"


def test_rotation_endpoint_forwards_window(client, monkeypatch):
    import lquant.industry as ind

    seen: list[tuple] = []

    def fake(asof=None, std=None, window=20):
        seen.append((asof, std, window))
        return {"asof": "2026-09-30", "std": "SW", "window": window,
                "rows": [], "notes": []}

    monkeypatch.setattr(ind, "industry_rotation", fake)
    r = client.get("/api/industry/rotation?window=60")
    assert r.status_code == 200
    assert seen[-1][2] == 60
    assert r.json()["window"] == 60


def test_rotation_rejects_out_of_range_window(client):
    assert client.get("/api/industry/rotation?window=3").status_code == 422
    assert client.get("/api/industry/rotation?window=999").status_code == 422


def test_rotation_is_not_shadowed_by_analysis_route(client, monkeypatch):
    """``/rotation`` 必须命中静态路由，而不是被 ``/{identifier}/analysis`` 吃掉。"""
    import lquant.industry as ind

    monkeypatch.setattr(ind, "industry_rotation",
                        lambda asof=None, std=None, window=20: {
                            "asof": "2026-09-30", "std": "SW", "window": window,
                            "rows": [], "notes": []})
    r = client.get("/api/industry/rotation")
    assert r.status_code == 200
    assert "rows" in r.json()
