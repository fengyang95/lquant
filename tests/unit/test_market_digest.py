"""自选股日报：编排语义（失败隔离、空清单、通知分类路由）。"""

from __future__ import annotations

import lquant.market.digest as dg
from lquant.market.digest import format_report, run_watchlist_digest


def make_report(symbol: str = "600519.SH", total: float = 62.0) -> dict:
    return {
        "symbol": symbol,
        "asof": "2026-09-30",
        "score": {"score": total, "grade": "中性偏多", "angle_coverage": 0.83},
        "verdict": {
            "points": ["综合 62 分（中性偏多）· 5/6 个角度有数据", "技术面 70 分，拉高综合分"],
            "risks": ["消息面缺失：无新闻源"],
        },
    }


class NotifySpy:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, title: str, text: str, *, category: str = "report", **kw):
        self.calls.append({"title": title, "text": text, "category": category})


# ---------- format_report ----------


def test_format_report_renders_score_points_risks():
    text = format_report(make_report())
    assert "【600519.SH】" in text and "62.0" in text
    assert "中性偏多" in text and "覆盖 83%" in text
    assert "· 综合 62 分" in text and "⚠ 消息面缺失" in text


def test_format_report_missing_fields_show_dash():
    text = format_report({"symbol": "000001.SZ"})
    assert "综合分 —" in text


# ---------- run_watchlist_digest ----------


def test_digest_sends_report_category_with_all_blocks():
    spy = NotifySpy()
    res = run_watchlist_digest(
        symbols=["600519.SH", "000001.SZ"], analyze_fn=lambda s, a: make_report(s), notify_fn=spy
    )
    assert res == {"sent": True, "ok": ["600519.SH", "000001.SZ"], "failed": [], "skipped": None}
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["category"] == "report"  # 日报走 report 通道
    assert "【600519.SH】" in call["text"] and "【000001.SZ】" in call["text"]


def test_digest_isolates_single_failure():
    """单只失败不影响其他；失败明细进返回值与正文提示，不静默。"""
    spy = NotifySpy()

    def flaky(sym, asof):
        if sym == "BAD.ST":
            raise RuntimeError("湖内无数据")
        return make_report(sym)

    res = run_watchlist_digest(
        symbols=["600519.SH", "BAD.ST", "000001.SZ"], analyze_fn=flaky, notify_fn=spy
    )
    assert res["sent"] is True
    assert res["ok"] == ["600519.SH", "000001.SZ"]
    assert res["failed"] == [{"symbol": "BAD.ST", "error": "RuntimeError: 湖内无数据"}]
    assert "BAD.ST" in spy.calls[0]["text"]  # 正文尾部有失败提示


def test_digest_all_failed_sends_error_category():
    spy = NotifySpy()
    res = run_watchlist_digest(
        symbols=["A", "B"],
        analyze_fn=lambda s, a: (_ for _ in ()).throw(ValueError("无数据")),
        notify_fn=spy,
    )
    assert res["sent"] is False and len(res["failed"]) == 2
    assert spy.calls[0]["category"] == "error"  # 全失败 → error 通道，不发空正文


def test_digest_empty_watchlist_skips_notify():
    spy = NotifySpy()
    res = run_watchlist_digest(symbols=[], analyze_fn=None, notify_fn=spy)
    assert res["sent"] is False and res["skipped"] == "清单为空"
    assert spy.calls == []  # 空清单不发空报告


def test_digest_defaults_to_watchlist_table(monkeypatch):
    """symbols 缺省读 watchlist 表；analyze/notify 缺省走真实入口（此处打桩）。"""
    monkeypatch.setattr(dg, "_watchlist_symbols", lambda: ["600519.SH"])
    spy = NotifySpy()
    res = run_watchlist_digest(analyze_fn=lambda s, a: make_report(s), notify_fn=spy)
    assert res["ok"] == ["600519.SH"]
    assert spy.calls[0]["category"] == "report"


# ---------- 审阅修复回归：sent 按真实送达判定 ----------


class SuppressedNotify:
    """模拟通知被降噪压制 / 通道全挂：返回 ok=False 的结果对象。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, title, text, *, category="report", **kw):
        self.calls += 1
        from types import SimpleNamespace

        return [SimpleNamespace(ok=False, skipped=True, channel="suppressed")]


def test_digest_sent_false_when_notification_suppressed():
    """通知被压制时 sent 如实为 False —— 「已生成」不冒充「已送达」。"""
    spy = SuppressedNotify()
    res = run_watchlist_digest(
        symbols=["600519.SH"], analyze_fn=lambda s, a: make_report(s), notify_fn=spy
    )
    assert res["sent"] is False
    assert res["ok"] == ["600519.SH"]  # 分析本身是成功的
    assert spy.calls == 1


def test_digest_sent_true_when_delivered():
    """真实送达（ok=True）→ sent=True；mock 无返回值的注入方保持兼容。"""
    spy = NotifySpy()
    res = run_watchlist_digest(
        symbols=["600519.SH"], analyze_fn=lambda s, a: make_report(s), notify_fn=spy
    )
    assert res["sent"] is True
