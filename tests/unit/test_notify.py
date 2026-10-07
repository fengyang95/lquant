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
import zoneinfo

import pytest

from lquant.notify.channels import (
    DingTalkBot,
    FeishuBot,
    GenericWebhook,
    NtfyChannel,
    PushPlusChannel,
    SendResult,
    ServerChan3Channel,
    TelegramBot,
    WecomBot,
    _business_error,
    slice_text,
)
from lquant.notify.service import (
    build_chain,
    format_results,
    in_quiet_hours,
    notify,
    reset_suppress_state,
    route_channels,
    severity_rank,
    should_suppress,
)

# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每用例从零配置开始：通知是 env 驱动的，别让上个用例的配置串进来。

    降噪类 env 也要清 —— 开发机若设了 MIN_SEVERITY/QUIET_HOURS 等，
    suppress 类用例会出现环境依赖性失败。
    """
    for k in (
        "LQ_NOTIFY_CHANNELS",
        "LQ_WECOM_WEBHOOK_URL",
        "LQ_FEISHU_WEBHOOK_URL",
        "LQ_DINGTALK_WEBHOOK_URL",
        "LQ_DINGTALK_SECRET",
        "LQ_TELEGRAM_BOT_TOKEN",
        "LQ_TELEGRAM_CHAT_ID",
        "LQ_GENERIC_WEBHOOK_URL",
        "LQ_NOTIFY_TIMEOUT",
        "LQ_NOTIFY_TEXT_LIMIT",
        "LQ_NOTIFY_MIN_SEVERITY",
        "LQ_NOTIFY_QUIET_HOURS",
        "LQ_NOTIFY_DEDUP_TTL_SECONDS",
        "LQ_NOTIFY_COOLDOWN_SECONDS",
        "LQ_NOTIFY_REPORT_CHANNELS",
        "LQ_NOTIFY_ALERT_CHANNELS",
        "LQ_NOTIFY_ERROR_CHANNELS",
    ):
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
    monkeypatch.setenv(
        "LQ_DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=k"
    )
    monkeypatch.setenv("LQ_DINGTALK_SECRET", "SECabc")
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.0)

    ch = DingTalkBot()
    url = ch._post_url(ch._url())
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["timestamp"] == ["1700000000000"]

    expected = base64.b64encode(
        hmac.new(b"SECabc", b"1700000000000\nSECabc", digestmod=hashlib.sha256).digest()
    ).decode()
    # parse_qs 已做 URL 解码 → 与原始签名直等；原始 URL 里的 '=' 必须已编码
    assert q["sign"] == [expected]
    assert "%3D" in url  # base64 尾部 '=' 已被 quote_plus 编码


def test_dingtalk_without_secret_keeps_url():
    url = "https://oapi.dingtalk.com/robot/send?access_token=k"
    assert DingTalkBot()._post_url(url) == url


# ---------- 降级语义 ----------


def test_unconfigured_channel_is_skipped_not_failed():
    r = WecomBot().send("t", "x")
    assert r.skipped and not r.ok and "未配置" in r.error


def test_notify_without_any_channel_returns_single_skip():
    results = notify("t", "x")  # LQ_NOTIFY_CHANNELS 未设
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
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "feishuu")  # 拼错
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
    assert {c.name for c in build_chain(["wecom", "dingtalk"])} == {"wecom", "dingtalk"}


def test_format_results_summary():
    s = format_results(
        [SendResult("wecom", ok=True), SendResult("feishu", ok=False, error="errcode=1: bad")]
    )
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

    monkeypatch.setattr(
        quotes,
        "fetch_snapshot",
        lambda syms: [
            {
                "symbol": "600000.SH",
                "name": "浦发银行",
                "price": price,
                "pre_close": 10.0,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "suspended": False,
            }
        ],
    )


def _patch_daily(monkeypatch, close=10.5):
    import polars as pl

    def fake_read_daily(symbols=None, start=None, end=None):
        return pl.DataFrame({"symbol": ["600000.SH"], "close": [close]}).lazy()

    monkeypatch.setattr("lquant.data.store.parquet.read_daily", fake_read_daily)


def test_day_close_critical_sends_notification(monkeypatch):
    """对账 critical 必须触发通知旁路 —— 这是本模块接线的核心场景。"""
    from lquant.paper import service

    fired: list[dict] = []

    def fake_notify(title, text, **kw):
        fired.append({"title": title, "text": text, **kw})
        return [SendResult("wecom", ok=True)]

    monkeypatch.setattr("lquant.notify.notify", fake_notify)
    service.create_account("nc1", 1_000_000)
    service.submit_order("nc1", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("nc1")
    _patch_daily(monkeypatch, close=5.0)  # 官方价与盯市价严重背离
    out = service.day_close("nc1")
    assert out["reconcile"]["verdict"] == "critical"
    assert len(fired) == 1
    assert "nc1" in fired[0]["title"] and "critical" in fired[0]["text"]
    # 对账背离是告警不是报告：必须走 alert 通道路由并带足 severity，
    # 否则 LQ_NOTIFY_ALERT_CHANNELS 路由与深夜静默豁免对它全部失效。
    assert fired[0]["category"] == "alert"
    assert fired[0]["severity"] == "critical"


def test_day_close_ok_is_silent(monkeypatch):
    """verdict=ok 不发通知 —— 别把 IM 群灌满每日噪音。"""
    from lquant.paper import service

    fired: list = []

    monkeypatch.setattr(
        "lquant.notify.notify", lambda title, text, **kw: fired.append((title, text))
    )
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
    out = service.day_close("nc3")  # 不抛
    assert out["reconcile"]["verdict"] == "critical"


# ==================== v1.5：分片 / 飞书加签 / 新渠道 / 路由 / 降噪 ====================


@pytest.fixture(autouse=True)
def _reset_suppress():
    """降噪是进程内状态：每个用例从零开始，防止跨用例串扰。"""
    reset_suppress_state()
    yield
    reset_suppress_state()


# ---------- 分片 ----------


def test_slice_text_short_message_single_slice():
    assert slice_text("t", "abc", 100) == [("t", "abc")]


def test_slice_text_long_message_marks_continuation():
    slices = slice_text("标题", "x" * 250, 100)
    assert len(slices) == 3
    assert slices[0][0] == "标题"
    assert slices[1][0] == "[续 1/3]" and slices[2][0] == "[续 2/3]"
    for _, chunk in slices:
        assert len(chunk) <= 100


def test_slice_text_no_limit_passes_through():
    assert slice_text("t", "x" * 9999, None) == [("t", "x" * 9999)]


def test_send_slices_long_text_and_stops_on_error(captured, monkeypatch):
    """分片续发：全片送达；中途失败即停，不重复发剩余片。"""
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv(
        "LQ_GENERIC_WEBHOOK_URL", "https://x/hook"
    )  # generic max_chars=None → 不分片

    class _Sliced(GenericWebhook):
        name = "webhook"
        max_chars = 50

    res = _Sliced().send("t", "y" * 120)  # body_cap=50-10-1=39 → 4 片
    assert res.ok and len(captured) == 4
    titles = [b[1]["title"] for b in captured]
    assert titles[0] == "t" and titles[1] == "[续 1/4]"

    captured.clear()

    class _FailSecond(_Sliced):
        pass

    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/fail-500")
    # 第一片成功（fail-500 也成功？不——fail-500 全部失败）→ 换真实策略：
    # 用自定义 urlopen 让第 2 片失败
    state = {"n": 0}

    def flaky(req, timeout=None):
        state["n"] += 1
        if state["n"] == 2:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", None, None)
        return _FakeResp('{"errcode":0}')

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    res2 = _Sliced().send("t", "y" * 120)
    assert not res2.ok and "429" in (res2.error or "")
    assert state["n"] == 2  # 第 2 片失败 → 第 3 片不再发送


# ---------- 飞书加签 ----------


def test_feishu_signing_uses_string_to_sign_as_key(monkeypatch):
    monkeypatch.setenv("LQ_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("LQ_FEISHU_WEBHOOK_SECRET", "SECf")
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)

    ch = FeishuBot()
    url = ch._post_url(ch._url())
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["timestamp"] == ["1700000000"]
    # 飞书口径：key = f"{ts}\n{secret}"，message 为空串
    expected = base64.b64encode(
        hmac.new(b"1700000000\nSECf", b"", digestmod=hashlib.sha256).digest()
    ).decode()
    assert q["sign"] == [expected]


# ---------- 新渠道 payload ----------


def test_ntfy_request_uses_plain_text_with_title_header():
    body, headers = NtfyChannel()._request("标题", "正文")
    assert body.decode() == "正文"
    assert headers["Content-Type"].startswith("text/plain")
    assert "X-Title" in headers  # 非 ASCII 标题被 ascii-ignore 处理，键必在


def test_pushplus_payload():
    assert PushPlusChannel()._payload("t", "c")["template"] == "txt"


def test_serverchan3_url_embeds_sendkey(monkeypatch):
    monkeypatch.setenv("LQ_SERVERCHAN3_SENDKEY", "SCT123")
    assert ServerChan3Channel()._url() == "https://SCT123.push.ft07.com/send"


def test_registry_contains_v15_channels():
    assert {"ntfy", "pushplus", "serverchan3"} <= set(
        __import__("lquant.notify.service", fromlist=["CHANNEL_FACTORY"]).CHANNEL_FACTORY
    )


# ---------- 分类路由 ----------


def _chain(*names):
    from lquant.notify.service import CHANNEL_FACTORY

    return [CHANNEL_FACTORY[n]() for n in names]


def test_route_without_env_keeps_all(monkeypatch):
    chain = _chain("wecom", "feishu")
    assert route_channels(chain, "alert") == chain


def test_route_narrows_by_category(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_ALERT_CHANNELS", "wecom")
    chain = _chain("wecom", "feishu")
    assert [c.name for c in route_channels(chain, "alert")] == ["wecom"]
    assert [c.name for c in route_channels(chain, "report")] == ["wecom", "feishu"]


def test_notify_category_routes_only_alert_channel(captured, monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "wecom,feishu")
    monkeypatch.setenv("LQ_WECOM_WEBHOOK_URL", "https://x/wecom")
    monkeypatch.setenv("LQ_FEISHU_WEBHOOK_URL", "https://x/feishu")
    monkeypatch.setenv("LQ_NOTIFY_ALERT_CHANNELS", "wecom")
    results = notify("对账", "critical", category="alert", severity="critical")
    assert [r.channel for r in results] == ["wecom"]  # feishu 被路由收窄


def test_notify_category_without_match_reports_skip(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "wecom")
    monkeypatch.setenv("LQ_NOTIFY_ERROR_CHANNELS", "feishu")
    results = notify("err", "x", category="error")
    assert results[0].skipped and "路由后无渠道" in results[0].error


# ---------- 降噪 ----------


def test_severity_rank_ordering():
    assert severity_rank("info") < severity_rank("warning") < severity_rank("critical")
    assert severity_rank("bogus") == 0  # 未知级别按 info 兜底


def test_suppress_min_severity(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_MIN_SEVERITY", "warning")
    assert should_suppress("t", "x", "info") == "min_severity"
    assert should_suppress("t", "x", "critical") is None


def test_suppress_quiet_hours_exempts_critical(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_QUIET_HOURS", "23:00-08:00")
    # 2026-09-30 是周三；01:00 落在跨午夜静默区间
    night = datetime.datetime(2026, 9, 30, 1, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Shanghai"))
    noon = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Shanghai"))
    assert in_quiet_hours(night) and not in_quiet_hours(noon)
    # should_suppress 用真实时钟，钉住"正在静默"再断言豁免关系
    import lquant.notify.service as nsvc

    monkeypatch.setattr(nsvc, "in_quiet_hours", lambda now=None: True)
    assert should_suppress("t", "x", "info") == "quiet_hours"
    assert should_suppress("t", "x", "critical") is None  # critical 豁免


def test_suppress_dedup_within_ttl(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    assert should_suppress("t", "x") is None  # 首次放行
    import lquant.notify.service as nsvc

    with nsvc._SUPPRESS_LOCK:
        nsvc._mark_sent("t", "x")  # 直接登记（notify 全失败会撤销）
    assert should_suppress("t", "x") == "dedup"  # TTL 内重复压制
    assert should_suppress("t", "different") is None  # 内容不同不压制


def test_suppress_cooldown(monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_COOLDOWN_SECONDS", "60")
    import lquant.notify.service as nsvc

    with nsvc._SUPPRESS_LOCK:
        nsvc._mark_sent("t1", "x")
    assert should_suppress("t2", "y") == "cooldown"  # 全局冷却与内容无关


def test_notify_suppressed_returns_reason(captured, monkeypatch):
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    notify("t", "x")
    n_before = len(captured)
    results = notify("t", "x")
    assert results[0].skipped and results[0].channel == "suppressed"
    assert len(captured) == n_before  # 被压制的消息不发网络请求


# ---------- 审阅修复回归：失败重试 / dedup 指纹 / 并发锁 / pushplus / 分片边界 ----------


def test_notify_failed_delivery_allows_retry(captured, monkeypatch):
    """全部通道失败 → 撤销降噪登记：重试不被 dedup/cooldown 吞掉（告警丢失比重复更糟）。"""
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    monkeypatch.setenv("LQ_NOTIFY_COOLDOWN_SECONDS", "60")

    def boom(req, timeout=None):
        raise OSError("网络故障")

    monkeypatch.setattr("lquant.notify.channels.urllib.request.urlopen", boom)
    r1 = notify("t", "x")
    assert r1[0].ok is False
    r2 = notify("t", "x")  # 重试：不得出现 suppressed 结果
    assert r2[0].ok is False and r2[0].skipped is False


def test_notify_success_keeps_dedup(captured, monkeypatch):
    """发送成功后登记保留：TTL 内重复消息被压制。"""
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    notify("t", "x")
    n = len(captured)
    results = notify("t", "x")
    assert results[0].channel == "suppressed" and len(captured) == n


def test_dedup_key_uses_truncated_text(captured, monkeypatch):
    """判定与登记用同一份（截断后的）文本 —— 否则超长消息去重指纹对不上。"""
    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "50")
    long_text = "x" * 200
    notify("t", long_text)
    n = len(captured)
    results = notify("t", long_text)  # 截断后与第一条完全相同 → 压制
    assert results[0].channel == "suppressed" and len(captured) == n


def test_concurrent_notify_dedupes_to_single_request(captured, monkeypatch):
    """server 线程池并发调 notify：check-then-act 原子化 → 相同内容只发一次。"""
    import concurrent.futures

    monkeypatch.setenv("LQ_NOTIFY_CHANNELS", "webhook")
    monkeypatch.setenv("LQ_GENERIC_WEBHOOK_URL", "https://x/hook")
    monkeypatch.setenv("LQ_NOTIFY_DEDUP_TTL_SECONDS", "300")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: notify("t", "same"), range(8)))
    assert len(captured) == 1
    oks = [r[0] for r in results if r[0].ok]
    suppressed = [r[0] for r in results if r[0].channel == "suppressed"]
    assert len(oks) == 1 and len(suppressed) == 7


def test_pushplus_response_codes():
    """pushplus 成功是 code=200（不是 errcode=0）；mock 的 errcode 格式走通用分支不受影响。"""
    from lquant.notify.channels import _business_error

    assert _business_error("pushplus", '{"code": 200, "msg": "请求成功"}') is None
    assert _business_error("pushplus", '{"code": 903, "msg": "无效token"}') is not None
    assert _business_error("pushplus", '{"errcode":0,"errmsg":"ok"}') is None  # 测试 mock 格式
    assert _business_error("wecom", '{"code": 200}') is not None  # 企微没有 code=200 语义


def test_slice_text_degenerate_max_chars_returns_single():
    """max_chars 装不下标题属配置错误：返回单片让平台报错，不抛 ValueError。"""
    from lquant.notify.channels import slice_text

    assert slice_text("t" * 20, "body", 10) == [("t" * 20, "body")]
