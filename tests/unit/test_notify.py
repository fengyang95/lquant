"""通知旁路单测：通道 payload、降级语义、并发发送、对账告警接线。

全部离线：urlopen monkeypatch，不打真实 webhook。
"""

import base64
import datetime
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from lquant.notify.channels import (
    DingTalkBot,
    FeishuBot,
    GenericWebhook,
    SendResult,
    TelegramBot,
    WecomBot,
    _business_error,
)
from lquant.notify.service import build_chain, format_results, notify

# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每用例从零配置开始：通知是 env 驱动的，别让上个用例的配置串进来。"""
    for k in ("LQ_NOTIFY_CHANNELS", "LQ_WECOM_WEBHOOK_URL",
              "LQ_FEISHU_WEBHOOK_URL", "LQ_DINGTALK_WEBHOOK_URL",
              "LQ_DINGTALK_SECRET", "LQ_TELEGRAM_BOT_TOKEN",
              "LQ_TELEGRAM_CHAT_ID", "LQ_GENERIC_WEBHOOK_URL",
              "LQ_NOTIFY_TIMEOUT", "LQ_NOTIFY_TEXT_LIMIT"):
        monkeypatch.delenv(k, raising=False)


class _FakeResp:
    def __init__(self, body: bytes | str = '{"errcode":0}'):
        self._b = body.encode() if isinstance(body, str) else body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def captured(monkeypatch):
    """记录每次 urlopen 的 (url, payload)，可按通道名注入失败。"""
    calls: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append((req.full_url, body))
        # 约定：URL 带 "fail-<code>" 时模拟 HTTP 失败，测单通道故障隔离
        if "fail-500" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)
        if "fail-timeout" in req.full_url:
            raise TimeoutError("timed out")
        return _FakeResp('{"errcode":0,"errmsg":"ok"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# ---------- payload schema ----------

def test_wecom_payload_schema():
    body = WecomBot()._payload("告警", "demo critical")
    assert body == {"msgtype": "text", "text": {"content": "告警\ndemo critical"}}


def test_feishu_payload_schema():
    body = FeishuBot()._payload("告警", "x")
    assert body["msg_type"] == "text" and body["content"]["text"] == "告警\nx"


def test_dingtalk_payload_schema():
    body = DingTalkBot()._payload("告警", "x")
    assert body == {"msgtype": "text", "text": {"content": "告警\nx"}}


def test_telegram_payload_schema(monkeypatch):
    monkeypatch.setenv("LQ_TELEGRAM_CHAT_ID", "12345")
    body = TelegramBot()._payload("告警", "x")
    assert body == {"chat_id": "12345", "text": "告警\nx"}


def test_generic_webhook_payload_schema():
    assert GenericWebhook()._payload("t", "x") == {"title": "t", "text": "x"}


# ---------- 钉钉加签 ----------

def test_dingtalk_signing_appends_timestamp_and_valid_sign(monkeypatch):
    monkeypatch.setenv("LQ_DINGTALK_WEBHOOK_URL",
                       "https://oapi.dingtalk.com/robot/send?access_token=k")
    monkeypatch.setenv("LQ_DINGTALK_SECRET", "SECabc")
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.0)

    ch = DingTalkBot()
    url = ch._post_url(ch._url())
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["timestamp"] == ["1700000000000"]

    expected = base64.b64encode(hmac.new(
        b"SECabc", b"1700000000000\nSECabc",
        digestmod=hashlib.sha256).digest()).decode()
    # parse_qs 已做 URL 解码 → 与原始签名直等；原始 URL 里的 '=' 必须已编码
    assert q["sign"] == [expected]
    assert "%3D" in url   # base64 尾部 '=' 已被 quote_plus 编码


def test_dingtalk_without_secret_keeps_url():
    url = "https://oapi.dingtalk.com/robot/send?access_token=k"
    assert DingTalkBot()._post_url(url) == url


# ---------- 降级语义 ----------

def test_unconfigured_channel_is_skipped_not_failed():
    r = WecomBot().send("t", "x")
    assert r.skipped and not r.ok and "未配置" in r.error


def test_notify_without_any_channel_returns_single_skip():
    results = notify("t", "x")   # LQ_NOTIFY_CHANNELS 未设
    assert len(results) == 1 and results[0].skipped


def test_business_error_parsing():
    assert _business_error("wecom", '{"errcode":93000,"errmsg":"invalid url"}')
    assert _business_error("telegram", '{"ok":false,"description":"bad token"}')
    assert _business_error("wecom", '{"errcode":0}') is None
    # 非协议响应（自建网关回 HTML）按 HTTP 成功放行
    assert _business_error("webhook", "<html>ok</html>") is None


def test_single_channel_failure_does_not_block_others(captured, monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "wecom,feishu")
    monkeypatch.setenv("LQ_WECOM_WEBHOOK_URL", "https://x/fail-500")
    monkeypatch.setenv("LQ_FEISHU_WEBHOOK_URL", "https://x/feishu")

    results = notify("t", "x")
    by = {r.channel: r for r in results}
    assert by["wecom"].ok is False and "HTTP 500" in by["wecom"].error
    assert by["feishu"].ok is True
    assert len(captured) == 2


def test_timeout_degrades_to_failed_result(captured, monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/fail-timeout")
    results = notify("t", "x")
    assert results[0].ok is False and "TimeoutError" in results[0].error


def test_unknown_channel_name_fails_loudly(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "feishuu")   # 拼错
    results = notify("t", "x")
    assert results[0].ok is False and "未注册" in results[0].error


def test_oversized_text_is_truncated(monkeypatch, captured):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "10")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    notify("t", "0123456789ABCDEF")
    _, payload = captured[0]
    assert len(payload["text"]) == 10 and payload["text"].endswith("…")


def test_build_chain_registry_roundtrip():
    assert {c.name for c in build_chain(["wecom", "dingtalk"])} == \
        {"wecom", "dingtalk"}


def test_format_results_summary():
    s = format_results([SendResult("wecom", ok=True),
                        SendResult("feishu", ok=False, error="errcode=1: bad")])
    assert s == "wecom=ok feishu=FAIL(errcode=1: bad)"


# ---------- day_close 对账告警接线 ----------

_FROZEN_DAY = datetime.date(2026, 9, 30)


@pytest.fixture(autouse=True)
def _paper_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_PAPER_DB", str(tmp_path / "paper.db"))
    # 通知单测同样要钉死交易日（test_paper_live.py 的既有纪律），
    # 否则周末/节假日跑CI时 intraday 净值不落库，对账用例随机翻车。
    from lquant.paper import service
    monkeypatch.setattr(service, "today_cn", lambda: _FROZEN_DAY)
    monkeypatch.setattr(service, "_is_trading_day", lambda d: True)


def _patch_quotes(monkeypatch, price=10.5):
    from lquant.paper import quotes
    monkeypatch.setattr(quotes, "fetch_snapshot", lambda syms: [
        {"symbol": "600000.SH", "name": "浦发银行", "price": price,
         "pre_close": 10.0, "limit_up": 11.0, "limit_down": 9.0,
         "suspended": False}])


def _patch_daily(monkeypatch, close=10.5):
    import polars as pl

    def fake_read_daily(symbols=None, start=None, end=None):
        return pl.DataFrame({"symbol": ["600000.SH"],
                             "close": [close]}).lazy()

    monkeypatch.setattr("lquant.data.store.parquet.read_daily", fake_read_daily)


def test_day_close_critical_sends_notification(monkeypatch):
    """对账 critical 必须触发通知旁路 —— 这是本模块接线的核心场景。"""
    from lquant.paper import service
    fired: list[tuple[str, str]] = []

    def fake_notify(title, text, **kw):
        fired.append((title, text))
        return [SendResult("wecom", ok=True)]

    monkeypatch.setattr("lquant.notify.notify", fake_notify)
    service.create_account("nc1", 1_000_000)
    service.submit_order("nc1", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("nc1")
    _patch_daily(monkeypatch, close=5.0)   # 官方价与盯市价严重背离
    out = service.day_close("nc1")
    assert out["reconcile"]["verdict"] == "critical"
    assert len(fired) == 1
    title, text = fired[0]
    assert "nc1" in title and "critical" in text


def test_day_close_ok_is_silent(monkeypatch):
    """verdict=ok 不发通知 —— 别把 IM 群灌满每日噪音。"""
    from lquant.paper import service
    fired: list = []

    monkeypatch.setattr("lquant.notify.notify",
                        lambda title, text, **kw: fired.append((title, text)))
    service.create_account("nc2", 1_000_000)
    service.submit_order("nc2", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("nc2")
    _patch_daily(monkeypatch, close=10.5)
    out = service.day_close("nc2")
    assert out["reconcile"]["verdict"] == "ok"
    assert fired == []


def test_day_close_notify_failure_never_breaks_reconcile(monkeypatch):
    """通知通道全炸也不许影响对账结果 —— 旁路的底线。"""
    from lquant.paper import service

    def boom(title, text, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("lquant.notify.notify", boom)
    service.create_account("nc3", 1_000_000)
    service.submit_order("nc3", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("nc3")
    _patch_daily(monkeypatch, close=5.0)
    out = service.day_close("nc3")   # 不抛
    assert out["reconcile"]["verdict"] == "critical"
