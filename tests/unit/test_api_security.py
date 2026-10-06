"""个股分析 API 契约测试（/api/security/*）。

用例只测 HTTP 边界与参数归一，不碰真实数据湖：``analyze_security`` 被打桩，
因为「分析内容对不对」由 ``test_security_analysis.py`` 覆盖，这里关心的是
代码归一、日期校验、错误码和响应形状。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_security")
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
    """打桩 analyze_security，记录收到的 (symbol, asof)。"""
    import lquant.security as sec

    seen: list[tuple[str, object]] = []

    def fake(symbol, asof=None):
        seen.append((symbol, asof))
        return {"symbol": str(symbol), "asof": "2026-09-30",
                "score": {"score": 60.0, "grade": "偏多"},
                "angles": [], "verdict": {"points": [], "risks": []},
                "overview": {}, "risk": {}, "schema_version": "1.0"}

    monkeypatch.setattr(sec, "analyze_security", fake)
    return seen


def test_bare_code_is_normalized(client, captured):
    """用户手输裸 6 位是高频操作 —— 必须在 API 边界归一。"""
    r = client.get("/api/security/600519/analysis")
    assert r.status_code == 200
    assert captured[0][0] == "600519.SH"


def test_full_code_passthrough(client, captured):
    r = client.get("/api/security/600519.SH/analysis")
    assert r.status_code == 200
    assert captured[0][0] == "600519.SH"


def test_shenzhen_bare_code(client, captured):
    r = client.get("/api/security/000001/analysis")
    assert r.status_code == 200
    assert captured[0][0] == "000001.SZ"


def test_asof_is_parsed_to_date(client, captured):
    from datetime import date

    r = client.get("/api/security/600519/analysis?asof=2026-06-30")
    assert r.status_code == 200
    assert captured[0][1] == date(2026, 6, 30)


def test_asof_omitted_means_none(client, captured):
    client.get("/api/security/600519/analysis")
    assert captured[0][1] is None


def test_invalid_symbol_is_422(client):
    assert client.get("/api/security/zzz/analysis").status_code == 422
    assert client.get("/api/security/1/analysis").status_code == 422


def test_invalid_asof_is_422(client):
    assert client.get("/api/security/600519/analysis?asof=2026-13-01").status_code == 422
    assert client.get("/api/security/600519/analysis?asof=notadate").status_code == 422


def test_angles_registry(client):
    r = client.get("/api/security/angles")
    assert r.status_code == 200
    body = r.json()
    ids = [a["id"] for a in body["angles"]]
    assert "technical" in ids and "fundamental" in ids and "news" in ids
    # 非零权重必须归一（综合分的尺度前提）
    nonzero = sum(a["weight"] for a in body["angles"] if a["weight"] > 0)
    assert nonzero == pytest.approx(1.0)
    assert body["schema_version"]


def test_analysis_returns_full_report_contract(client, captured):
    r = client.get("/api/security/600519/analysis")
    assert r.status_code == 200
    body = r.json()
    for key in ("symbol", "asof", "overview", "score", "verdict", "angles",
                "risk", "schema_version"):
        assert key in body, f"报告缺少字段 {key}"
