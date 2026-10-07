"""告警规则引擎单测：CRUD 往返 / 评估口径 / 冷却持久化 / run_rules 编排 / API。

全部离线：sqlite 落 tmp_path，通知走 monkeypatch 替身。
"""

from fastapi.testclient import TestClient

from lquant.notify.rules import (
    COOLDOWN,
    EVAL_ERROR,
    NOT_TRIGGERED,
    TRIGGERED,
    AlertRule,
    RuleStore,
    evaluate,
    get_store,
    run_rules,
)


def test_validate_catches_bad_fields():
    errs = AlertRule(
        name="", alert_type="bogus", severity="x", parameters={"threshold": "abc"}
    ).validate()
    assert len(errs) == 4  # name / alert_type / severity / threshold


def test_store_crud_roundtrip(tmp_path):
    store = RuleStore(tmp_path / "r.db")
    rule = store.add(
        AlertRule(
            name="浦发破位",
            target="600000.SH",
            alert_type="price_below",
            parameters={"threshold": 10.0},
        )
    )
    assert rule.id is not None
    got = store.get(rule.id)
    assert got.name == "浦发破位" and got.parameters["threshold"] == 10.0
    upd = store.update(rule.id, severity="critical", enabled=False)
    assert upd.severity == "critical" and upd.enabled is False
    assert store.list(enabled_only=True) == []
    assert store.list() == [upd]
    assert store.delete(rule.id) and store.get(rule.id) is None
    assert not store.delete(rule.id)  # 幂等


def test_evaluate_price_types():
    above = AlertRule(name="a", alert_type="price_above", parameters={"threshold": 10.0})
    below = AlertRule(name="b", alert_type="price_below", parameters={"threshold": 10.0})
    assert evaluate(above, {"last_price": 10.5})[0] == TRIGGERED
    assert evaluate(above, {"last_price": 9.9})[0] == NOT_TRIGGERED
    assert evaluate(below, {"last_price": 9.9})[0] == TRIGGERED


def test_evaluate_pct_change():
    up = AlertRule(name="u", alert_type="pct_change_up", parameters={"threshold": 5.0})
    down = AlertRule(name="d", alert_type="pct_change_down", parameters={"threshold": 5.0})
    ctx = {"last_price": 10.6, "pre_close": 10.0}  # +6%
    assert evaluate(up, ctx)[0] == TRIGGERED
    assert evaluate(down, {"last_price": 9.4, "pre_close": 10.0})[0] == TRIGGERED
    assert evaluate(up, {"last_price": 10.2, "pre_close": 10.0})[0] == NOT_TRIGGERED


def test_evaluate_errors_are_loud_not_silent():
    rule = AlertRule(name="a", alert_type="price_above", parameters={"threshold": 10.0})
    status, detail = evaluate(rule, {})  # 缺字段
    assert status == EVAL_ERROR and "缺字段" in detail
    status, _ = evaluate(rule, {"last_price": "abc"})  # 数值非法
    assert status == EVAL_ERROR
    status, _ = evaluate(rule, {"last_price": 9.0, "pre_close": 0})
    # pct 类型才吃 pre_close；price_above 只看 last_price → 9 < 10 正常未触发
    assert status == NOT_TRIGGERED


def test_cooldown_persists_and_blocks(tmp_path):
    store = RuleStore(tmp_path / "r.db")
    rule = store.add(
        AlertRule(
            name="a",
            alert_type="price_above",
            parameters={"threshold": 10.0},
            cooldown_seconds=3600,
        )
    )
    store.mark_triggered(rule.id, 3600)
    got = store.get(rule.id)
    assert got.cooldown_until is not None
    status, detail = evaluate(got, {"last_price": 99.0})
    assert status == COOLDOWN and "冷却至" in detail


def test_run_rules_triggers_and_notifies(tmp_path):
    store = RuleStore(tmp_path / "r.db")
    store.add(
        AlertRule(
            name="浦发涨停预警",
            target="600000.SH",
            alert_type="pct_change_up",
            parameters={"threshold": 5.0},
            cooldown_seconds=300,
        )
    )
    fired: list[tuple] = []

    def fake_notify(title, text, **kw):
        fired.append((title, text, kw))

    ctxs = [
        {"symbol": "600000.SH", "last_price": 10.6, "pre_close": 10.0},
        {"symbol": "000001.SZ", "last_price": 10.0, "pre_close": 10.0},
    ]
    results = run_rules(ctxs, store=store, notify_fn=fake_notify)
    assert results[0]["status"] == TRIGGERED
    assert len(fired) == 1 and "600000.SH" in fired[0][1]
    assert fired[0][2]["category"] == "alert"
    # 第二次跑同 ctx：冷却中 → 不重复触发不重复发（冷却持久化的意义）
    results2 = run_rules(ctxs, store=store, notify_fn=fake_notify)
    assert results2[0]["status"] == COOLDOWN and len(fired) == 1


def test_run_rules_market_scope_matches_all(tmp_path):
    store = RuleStore(tmp_path / "r.db")
    store.add(
        AlertRule(
            name="全市场大涨",
            target_scope="market",
            target="",
            alert_type="pct_change_up",
            parameters={"threshold": 5.0},
        )
    )
    fired: list = []
    results = run_rules(
        [{"symbol": "510300.SH", "last_price": 4.3, "pre_close": 4.0}],
        store=store,
        notify_fn=lambda *a, **k: fired.append(a),
    )
    assert results[0]["status"] == TRIGGERED and len(fired) == 1


def test_get_store_rebuilds_on_env_change(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_DB", str(tmp_path / "a.db"))
    s1 = get_store()
    monkeypatch.setenv("LQ_NOTIFY_DB", str(tmp_path / "b.db"))
    assert get_store() is not s1


# ---------- API ----------


def make_client(tmp_path, monkeypatch) -> TestClient:
    from fastapi import FastAPI

    from lquant.server.api import notify as notify_api

    monkeypatch.setenv("LQ_NOTIFY_DB", str(tmp_path / "api.db"))
    app = FastAPI()
    app.include_router(notify_api.router, prefix="/api")
    return TestClient(app)


def test_api_rule_lifecycle(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    body = {
        "name": "ETF跌破",
        "target": "510300.SH",
        "alert_type": "price_below",
        "parameters": {"threshold": 4.0},
        "severity": "warning",
    }
    r = c.post("/api/notify/rules", json=body)
    assert r.status_code == 200
    rid = r.json()["id"]

    r = c.post(f"/api/notify/rules/{rid}/evaluate", json={"ctx": {"last_price": 3.9}})  # DryRun
    assert r.status_code == 200 and r.json()["status"] == TRIGGERED
    # DryRun 不落冷却：连跑两次结果一致
    r2 = c.post(f"/api/notify/rules/{rid}/evaluate", json={"ctx": {"last_price": 3.9}})
    assert r2.json()["status"] == TRIGGERED

    assert (
        c.patch(f"/api/notify/rules/{rid}", json={"severity": "critical"}).json()["severity"]
        == "critical"
    )
    assert c.get("/api/notify/rules").json()["rules"][0]["name"] == "ETF跌破"
    assert c.delete(f"/api/notify/rules/{rid}").status_code == 200
    assert c.delete(f"/api/notify/rules/{rid}").status_code == 404


def test_api_rejects_invalid_rule(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    r = c.post("/api/notify/rules", json={"name": "坏规则", "alert_type": "bogus"})
    assert r.status_code == 422 and "alert_type" in r.json()["detail"]


def test_api_run_and_test_send(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    c.post(
        "/api/notify/rules",
        json={
            "name": "全市场",
            "target_scope": "market",
            "alert_type": "pct_change_up",
            "parameters": {"threshold": 5.0},
        },
    )
    r = c.post(
        "/api/notify/rules/run", json=[{"symbol": "510300.SH", "last_price": 4.3, "pre_close": 4.0}]
    )
    assert r.status_code == 200 and r.json()["results"][0]["status"] == TRIGGERED
    # /notify/test 走真实 notify 主链路：未配渠道 → skipped，但端点本身 200
    r = c.post("/api/notify/test", json={"title": "t", "text": "x"})
    assert r.status_code == 200 and r.json()["results"][0]["skipped"]


# ---------- 审阅修复回归：未实现的 scope 创建时就拒绝 ----------


def test_validate_rejects_unimplemented_scopes():
    """watchlist/portfolio scope 无法被 run_rules 实际触发（只按 symbol 匹配），
    配了却永远不响 = 静默失败，创建时就必须拒绝。"""
    r = AlertRule(
        name="w",
        target_scope="watchlist",
        target="默认池",
        alert_type="price_above",
        parameters={"threshold": 10.0},
    )
    problems = r.validate()
    assert any("暂不支持" in p for p in problems)
    # 已实现的 scope 不受影响
    ok_rule = AlertRule(
        name="s",
        target_scope="single_symbol",
        target="600519.SH",
        alert_type="price_above",
        parameters={"threshold": 10.0},
    )
    assert ok_rule.validate() == []
