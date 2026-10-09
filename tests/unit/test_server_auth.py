"""开放接口 Token 鉴权 + scope 分档 + 契约快照测试（FEATURE-IDEAS C1）。

鉴权模块最危险的失败模式不是「报错」，而是**静默放行**：漏一条规则、顺序写反、
或者把 query 参数当凭证收下，都不会有任何异常，只会安静地少一层防线。
所以这里每一个用例都在钉死一条「不许退化」的性质：

- 未配置 token = 与加鉴权之前完全一致（本地单机零影响，这是硬要求）
- 配置了 token：无凭证 401 / scope 不足 403 / scope 够 200 / 未开放 403
- 规则表顺序敏感：具体规则必须压过宽泛规则
- 前缀匹配按 ``/`` 段边界（否则 /api/data 会漏成 /api/database-evil）
- 只存 SHA-256、吊销即时生效、query 传 token 一律不认
- 开放面快照：任何变化都必须是有意识的

不在本文件里跑真实端点语义（那由 test_api* 负责），这里只测鉴权边界。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试环境不启动同步后台线程（与本仓库其他 API 测试一致）
os.environ.setdefault("LQ_SYNC_WORKER", "0")

from lquant.server import auth  # noqa: E402

SNAPSHOT_PATH = Path(__file__).with_name("server_auth_open_surface.json")

# 代表性端点（都不碰 DB，所以用例可脱离数据湖跑）：
PING_MARKET = "/api/data/ping"          # read:market
FIELDS_FACTORS = "/api/factors/fields"  # read:factors
HEALTH = "/api/health/ping"             # * （仅需有效凭证）
DEFAULT_DENIED = "/api/agent/capabilities"  # 不在规则表 = 默认拒绝


# ── 夹具 ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def tokens_file(tmp_path_factory):
    """把 token 库指到模块私有 tmp 文件。

    必须走 ``LQ_API_TOKENS_PATH`` 覆盖：默认路径是「数据根 / api_tokens.json」，
    要是往那儿写，跑一次测试就会把开发机的服务切成强制鉴权模式。
    """
    path = tmp_path_factory.mktemp("auth") / "api_tokens.json"
    old = os.environ.get("LQ_API_TOKENS_PATH")
    os.environ["LQ_API_TOKENS_PATH"] = str(path)
    try:
        yield path
    finally:
        if old is None:
            os.environ.pop("LQ_API_TOKENS_PATH", None)
        else:
            os.environ["LQ_API_TOKENS_PATH"] = old


@pytest.fixture(autouse=True)
def _clean_tokens(tokens_file):
    """每个用例从「没有任何 token」的干净状态开始，避免用例互相污染。"""
    tokens_file.unlink(missing_ok=True)
    yield
    tokens_file.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def client(tokens_file):  # noqa: ARG001 - 依赖它设置环境
    from lquant.server.main import create_app

    # token 库是「每请求读盘」的（吊销/过期必须立刻生效），所以一个 client
    # 就能跨用例切换「未配置 / 已配置」两种状态，不需要重建 app。
    with TestClient(create_app()) as c:
        yield c


def _new_token(name: str, scopes: list[str], *, expires_at: str | None = None):
    """建 token，返回 (明文, id)。"""
    view, plaintext = auth.create_token(name, scopes, expires_at)
    return plaintext, view["id"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 1. 未配置 token = 零行为变化 ──────────────────────────────────────────


def test_unconfigured_store_is_full_bypass(client):
    """没有 token 库文件时：鉴权整体旁路，既有端点行为与加鉴权之前一致。"""
    assert auth.load_store().enabled is False

    for path in (HEALTH, PING_MARKET, FIELDS_FACTORS, DEFAULT_DENIED):
        r = client.get(path)
        assert r.status_code not in (401, 403), (
            f"未配置 token 时 {path} 不应出现鉴权状态码，实际 {r.status_code}")


def test_unconfigured_mutations_still_pass_through(client):
    """写方法同样旁路（本地单机既有用法不许被打断）。"""
    r = client.post("/api/factors/validate", json={"expression": "close"})
    assert r.status_code == 200, r.text


# ── 2. 配置 token 后的四种情形（同一端点）────────────────────────────────


def test_no_credential_is_401(client, tokens_file):
    token, _ = _new_token("t", ["read:market"])
    r = client.get(PING_MARKET)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    # 401 不泄漏内部细节：既不带路径信息，也不带 token 的名称/持有 scope
    assert token[:8] not in r.text
    assert "read:market" not in r.text


def test_insufficient_scope_is_403(client):
    token, _ = _new_token("reader", ["read:factors"])
    r = client.get(PING_MARKET, headers=_bearer(token))
    assert r.status_code == 403
    assert "read:market" in r.json()["detail"]  # 需求是公开契约，可以回


def test_sufficient_scope_is_200(client):
    token, _ = _new_token("market", ["read:market"])
    assert client.get(PING_MARKET, headers=_bearer(token)).status_code == 200
    token2, _ = _new_token("researcher", ["read:factors"])
    assert client.get(FIELDS_FACTORS, headers=_bearer(token2)).status_code == 200


def test_any_scope_token_reaches_liveness(client):
    """健康探针只要求「持有有效凭证」，不附带任何数据权限。"""
    token, _ = _new_token("probe", ["read:factors"])
    assert client.get(HEALTH, headers=_bearer(token)).status_code == 200


def test_x_api_token_header_also_accepted(client):
    token, _ = _new_token("hdr", ["read:market"])
    r = client.get(PING_MARKET, headers={"X-API-Token": token})
    assert r.status_code == 200


# ── 3. 规则表顺序敏感：具体规则压过宽泛规则 ──────────────────────────────


def test_specific_rule_beats_broad_rule():
    """真实重叠：POST /api/factors（write:data）vs 其子路径。

    顺序写反的后果不是报错，而是「一个只有写因子权限的 token 触发了集群计算」。
    """
    assert auth.required_scope("POST", "/api/factors") == "write:data"
    assert auth.required_scope("POST", "/api/factors/evaluate") == "run:backtest"
    assert auth.required_scope("POST", "/api/factors/evaluate/series") == "run:backtest"
    assert auth.required_scope("POST", "/api/factors/ast") == "read:factors"
    assert auth.required_scope("POST", "/api/strategies") == "write:data"
    assert auth.required_scope("POST", "/api/strategies/validate") == "read:factors"


def test_order_sensitivity_at_http_layer(client):
    """用无害端点实测顺序：POST /api/factors/validate 是只读档。

    宽泛规则 POST /api/factors = write:data 若排在前面，下面的 write token
    就会被放行（200）；具体规则胜出时它是 403。
    """
    write_token, _ = _new_token("writer", ["write:data"])
    r = client.post("/api/factors/validate", json={"expression": "close"},
                    headers=_bearer(write_token))
    assert r.status_code == 403, "宽泛规则压过了具体规则：只读端点被写权限放行"

    read_token, _ = _new_token("reader", ["read:factors"])
    r2 = client.post("/api/factors/validate", json={"expression": "close"},
                     headers=_bearer(read_token))
    assert r2.status_code == 200


# ── 4. 默认拒绝（fail-closed）────────────────────────────────────────────


def test_unmatched_path_default_denied():
    """规则表没写的路径 = 不开放；即使把全部 scope 都给上也一样。"""
    assert auth.required_scope("GET", "/api/agent/capabilities") is None
    assert auth.required_scope("GET", "/api/does-not-exist") is None

    token, _ = _new_token("god", list(auth.SCOPES))
    d = auth.evaluate(DEFAULT_DENIED, "GET", token)
    assert d.status == 403


def test_default_denied_at_http_layer(client):
    token, _ = _new_token("god2", list(auth.SCOPES))
    assert client.get(DEFAULT_DENIED, headers=_bearer(token)).status_code == 403


def test_prefix_match_respects_segment_boundary():
    """/api/data 不能连带命中 /api/database-evil —— 前缀不按段切分就是绕过口。"""
    assert auth.required_scope("GET", "/api/database-evil") is None
    assert auth.required_scope("GET", "/api/data") == "read:market"
    assert auth.required_scope("GET", "/api/data/coverage") == "read:market"


def test_sse_stream_explicitly_closed():
    """EventSource 带不了 Authorization 头，所以 SSE 端点诚实关掉而非假装开放。"""
    assert auth.required_scope("GET", "/api/data/tasks/abc/events") is None


# ── 5. 管理面永远只认 admin ──────────────────────────────────────────────


def test_admin_surface_requires_admin(client):
    normal, _ = _new_token("normal", ["read:market", "read:factors", "write:data",
                                      "run:backtest", "paper:trade"])
    for path in ("/api/auth/tokens", "/api/settings", "/api/monitor/summary"):
        assert client.get(path, headers=_bearer(normal)).status_code == 403, path

    admin, _ = _new_token("root", ["admin"])
    assert client.get("/api/auth/tokens", headers=_bearer(admin)).status_code == 200


def test_management_endpoints_refuse_normal_token(client):
    """管理端点自身不允许普通 token —— 否则「给自己提权」是两步走的漏洞。"""
    normal, _ = _new_token("normal2", ["read:market", "write:data"])
    assert client.get("/api/auth/tokens", headers=_bearer(normal)).status_code == 403
    r = client.post("/api/auth/tokens", json={"name": "x", "scopes": ["admin"]},
                    headers=_bearer(normal))
    assert r.status_code == 403

    admin, _ = _new_token("root3", ["admin"])
    r2 = client.post("/api/auth/tokens", json={"name": "x", "scopes": ["admin"]},
                     headers=_bearer(admin))
    assert r2.status_code == 201, r2.text
    assert r2.json()["token"].startswith(auth.TOKEN_PREFIX)


# ── 6. 存储：只存哈希、列出不回明文 ──────────────────────────────────────


def test_token_stored_as_sha256_not_plaintext(tokens_file):
    plaintext, tid = _new_token("hashcheck", ["read:market"])
    raw = tokens_file.read_text(encoding="utf-8")

    assert plaintext not in raw, "token 库出现明文 —— 这是本模块的红线"
    assert hashlib.sha256(plaintext.encode()).hexdigest() in raw

    listed = auth.list_tokens()
    assert listed and listed[0]["id"] == tid
    assert "token_hash" not in listed[0] and "token" not in listed[0]


def test_created_token_plaintext_returned_once():
    view, plaintext = auth.create_token("once", ["read:market"])
    assert plaintext.startswith(auth.TOKEN_PREFIX)
    assert plaintext not in json.dumps(view)
    assert "token_hash" not in view


def test_list_api_never_echoes_hash(client):
    token, _ = _new_token("listed", ["read:market"])
    admin, _ = _new_token("root2", ["admin"])
    body = client.get("/api/auth/tokens", headers=_bearer(admin)).json()
    text = json.dumps(body)
    assert token not in text
    assert "token_hash" not in text
    assert body["auth_enabled"] is True


# ── 7. 吊销立即生效 ──────────────────────────────────────────────────────


def test_revoked_token_immediately_invalid(client):
    token, tid = _new_token("revokeme", ["read:market"])
    admin, _ = _new_token("revoker", ["admin"])

    assert client.get(PING_MARKET, headers=_bearer(token)).status_code == 200
    r = client.delete(f"/api/auth/tokens/{tid}", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert client.get(PING_MARKET, headers=_bearer(token)).status_code == 401
    assert client.delete("/api/auth/tokens/tok_nope",
                         headers=_bearer(admin)).status_code == 404


def test_expired_token_rejected():
    token, _ = _new_token("old", ["read:market"], expires_at="2000-01-01T00:00:00+08:00")
    assert auth.evaluate(PING_MARKET, "GET", token).status == 401


def test_create_rejects_unknown_scope_and_empty_scopes():
    with pytest.raises(ValueError, match="未知 scope"):
        auth.create_token("bad", ["read:everything"])
    with pytest.raises(ValueError, match="至少要给一个 scope"):
        auth.create_token("empty", [])


# ── 8. query 传 token 一律不认 ───────────────────────────────────────────


def test_query_param_token_not_accepted(client):
    """query 会进 access log / 浏览器历史 / Referer，等于把长期凭证写进日志。"""
    token, _ = _new_token("qq", ["read:market"])
    for q in (f"?token={token}", f"?api_token={token}", f"?access_token={token}"):
        r = client.get(PING_MARKET + q)
        assert r.status_code == 401, f"query 传参被接受了: {q}"


def test_extract_token_ignores_query_like_headers():
    from starlette.datastructures import Headers

    assert auth.extract_token(Headers({"authorization": "Bearer abc"})) == "abc"
    assert auth.extract_token(Headers({"x-api-token": "abc"})) == "abc"
    assert auth.extract_token(Headers({"authorization": "Basic abc"})) is None
    assert auth.extract_token(Headers({})) is None


# ── 9. 契约快照：开放面的任何变化都必须是有意识的 ────────────────────────


def _collect_open_surface() -> dict[str, str]:
    """与运行时同源：app.openapi() 的每个 (方法, 路径模板) 过规则表判定。

    唯一裁决函数是 ``auth.required_scope`` —— 快照、OpenAPI 契约视图和中间件
    读的是同一张表，不存在「快照说开放、实际拦下来」的漂移。
    """
    from lquant.server.main import MonitorMiddleware, create_app

    app = create_app()
    fastapi_app = app.app if isinstance(app, MonitorMiddleware) else app
    found: dict[str, str] = {}
    for path, ops in fastapi_app.openapi().get("paths", {}).items():
        for method in ops:
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            scope = auth.required_scope(method.upper(), path)
            if scope is not None:
                found[f"{method.upper()} {path}"] = scope
    return found


def test_open_surface_contract_snapshot():
    """冻结「开放面 = 方法 + 路径 + 所需 scope」。

    失败 = 契约变更。**有意识地更新**：
        LQ_UPDATE_AUTH_CONTRACT=1 .venv/bin/python -m pytest \\
            tests/unit/test_server_auth.py -k snapshot
    然后人工复核 diff（新增的是不是真的该开放？scope 定对了没有？），
    并在 commit message 里写明契约变更。
    """
    found = _collect_open_surface()

    if os.getenv("LQ_UPDATE_AUTH_CONTRACT") == "1":
        SNAPSHOT_PATH.write_text(
            json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        pytest.skip(f"已按当前规则表重写快照（{len(found)} 条）—— 请人工复核 diff")

    expected: dict[str, str] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    added = {k: v for k, v in found.items() if k not in expected}
    removed = {k: v for k, v in expected.items() if k not in found}
    changed = {k: (expected[k], found[k]) for k in found.keys() & expected.keys()
               if found[k] != expected[k]}
    if added or removed or changed:
        pytest.fail(
            "开放面契约发生变化 —— 这是对外承诺面，必须有意为之：\n"
            f"  新增: {json.dumps(added, ensure_ascii=False, sort_keys=True)}\n"
            f"  移除: {json.dumps(removed, ensure_ascii=False, sort_keys=True)}\n"
            f"  改档: {json.dumps(changed, ensure_ascii=False, sort_keys=True)}\n"
            "确认无误后用 LQ_UPDATE_AUTH_CONTRACT=1 重写 "
            f"{SNAPSHOT_PATH.name}，复核 diff 并在 commit 里说明契约变更。",
        )


def test_snapshot_is_not_trivially_empty():
    """快照本身要能挡住「规则表被清空」这种整体失效：开放面不能是空的。"""
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert len(expected) > 100
    assert set(expected.values()) <= set(auth.SCOPES) | {auth.SCOPE_ANY}


# ── 10. 时间/路径/坏文件的退化分支（覆盖率补齐） ─────────────────────────


def test_parse_ts_and_data_dir_branches(monkeypatch, tmp_path):
    assert auth._parse_ts("不是时间") is None
    assert auth._parse_ts(None) is None
    assert auth._parse_ts("2026-01-01T00:00:00").tzinfo is not None  # naive 补平台时区

    monkeypatch.setenv("LQ_DATA_DIR", "rel/data")
    assert auth._data_dir() == auth._root() / "rel/data"  # 相对路径锚定仓库根
    monkeypatch.setenv("LQ_DATA_DIR", str(tmp_path / "abs"))
    assert auth._data_dir() == tmp_path / "abs"
    monkeypatch.delenv("LQ_DATA_DIR", raising=False)
    assert isinstance(auth._data_dir(), Path)


def test_corrupt_token_store_fails_closed(client, tokens_file):
    """坏 token 文件按「已启用但无任何有效记录」处理 → 全部拒绝，不静默放行。"""
    tokens_file.write_text("{ 坏 JSON", encoding="utf-8")
    st = auth.load_store()
    assert st.enabled is True and st.records == ()
    assert auth._read_raw() == []
    assert auth.evaluate(PING_MARKET, "GET", "whatever").status == 401


def test_missing_credential_is_401_when_enabled():
    _new_token("t", ["read:market"])
    assert auth.evaluate(PING_MARKET, "GET", None).status == 401


def test_create_token_rejects_bad_expiry():
    with pytest.raises(ValueError, match="ISO 8601"):
        auth.create_token("bad", ["read:market"], expires_at="不是时间")


def test_chmod_failure_is_best_effort(monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("文件系统不支持 chmod")

    monkeypatch.setattr(auth.os, "chmod", boom)
    view, plaintext = auth.create_token("chmod", ["read:market"])
    assert plaintext.startswith(auth.TOKEN_PREFIX) and view["id"]


def test_options_preflight_bypasses_auth(client):
    _new_token("preflight", ["read:market"])
    assert client.request("OPTIONS", PING_MARKET).status_code != 401


def test_create_token_api_422_on_bad_scope(client):
    r = client.post("/api/auth/tokens", json={"name": "x", "scopes": ["nope"]})
    assert r.status_code == 422
