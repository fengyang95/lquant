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
    assert res["sent"] is True
    assert res["ok"] == ["600519.SH", "000001.SZ"]
    assert res["failed"] == [] and res["skipped"] is None
    # 两条一块 → 单页；全部送达 → sent_pages == pages
    assert res["pages"] == 1 and res["sent_pages"] == 1
    assert res["truncated"] is False and res["omitted_symbols"] == []
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


def test_watchlist_symbols_missing_table_is_empty_not_crash(tmp_path, monkeypatch):
    """``watchlist`` 表不存在时按空清单处理（cron 姿势下 UI 从没被打开过）。

    建表语句在 Web 端点里（首次访问才建），而 ``lq notify digest`` 的典型用法
    恰恰是无人的定时任务 —— 这里不能让 CatalogException 冒出去炸掉整个 cron。
    """
    import duckdb

    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(get_settings().duckdb_path))
        try:
            con.execute("CREATE TABLE security (symbol VARCHAR)")  # 库在，但没 watchlist
        finally:
            con.close()

        assert dg._watchlist_symbols() == []
        # 端到端：默认读自选清单的日报退化成「清单为空」，而不是异常
        spy = NotifySpy()
        res = run_watchlist_digest(analyze_fn=lambda s, a: make_report(s), notify_fn=spy)
        assert res["sent"] is False and res["ok"] == [] and res["failed"] == []
        assert res["skipped"] == "清单为空"
        assert res["pages"] == 0 and res["truncated"] is False
        assert spy.calls == []
    finally:
        # 收尾必须放 finally：中途任一条断言失败时若不还原，全局 settings
        # 缓存会残留 tmp 的 LQ_ROOT，后续用例连锁 FileNotFoundError。
        get_settings.cache_clear()




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


# ---------- 分页 / 截断披露（回归 HIGH：通知层静默截断） ----------


class CapturingChannel:
    """真实 notify 链上的假通道：记录实收正文，验证截断行为。"""

    name = "webhook"

    def __init__(self):
        self.texts: list[str] = []

    def send(self, title: str, text: str):
        from lquant.notify.channels import SendResult

        self.texts.append(text)
        return SendResult(self.name, ok=True, sent_parts=1)


def _clear_notify_env(monkeypatch):
    for k in (
        "LQ_NOTIFY_TEXT_LIMIT",
        "LQ_NOTIFY_REPORT_CHANNELS",
        "LQ_NOTIFY_MIN_SEVERITY",
        "LQ_NOTIFY_QUIET_HOURS",
        "LQ_NOTIFY_DEDUP_TTL_SECONDS",
        "LQ_NOTIFY_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_digest_failed_disclosure_survives_real_notify_truncation(monkeypatch):
    """60 只标的（正文远超 3500 字）时失败披露仍必须抵达渠道。

    旧实现把 N 只拼成一条无上限正文，notify() 在任何分片之前先
    ``text[:limit-1] + "…"``，拼在最尾部的「分析失败」是第一个被砍的内容，
    而 sent 仍按送达报 True。这里走**真实 notify**（只打桩通道）复现。
    """
    import lquant.notify.service as notify_service

    _clear_notify_env(monkeypatch)
    cap = CapturingChannel()
    monkeypatch.setattr(notify_service, "build_chain", lambda: [cap])

    syms = [f"6005{i:02d}.SH" for i in range(59)] + ["BAD.SH"]

    def analyze(s, a):
        if s == "BAD.SH":
            raise RuntimeError("湖内无数据")
        return make_report(s)

    res = run_watchlist_digest(symbols=syms, analyze_fn=analyze)
    assert res["sent"] is True
    assert res["pages"] > 1 and res["sent_pages"] == res["pages"]
    limit = dg._notify_text_limit()
    assert all(len(t) <= limit for t in cap.texts)  # 每页都不触发 notify 截断
    received = "".join(cap.texts)
    assert "分析失败" in received and "BAD.SH" in received  # 披露没被吃掉
    assert received.count("【") == len(res["ok"]) == 59  # 成功块一个不少


def test_digest_paginates_within_configured_limit(monkeypatch):
    """LQ_NOTIFY_TEXT_LIMIT 收紧到 400：按标的分页，页数/送达数如实回报。"""
    _clear_notify_env(monkeypatch)
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "400")
    spy = NotifySpy()
    syms = [f"{i:06d}.SH" for i in range(20)]
    res = run_watchlist_digest(symbols=syms, analyze_fn=lambda s, a: make_report(s), notify_fn=spy)
    assert res["sent"] is True
    assert res["pages"] == len(spy.calls) > 1
    assert res["sent_pages"] == res["pages"]
    assert all(len(c["text"]) <= 400 for c in spy.calls)
    assert set(res["ok"]) == set(syms)


def test_digest_partial_page_delivery_reports_sent_false(monkeypatch):
    """部分页送达：sent 如实 False，并用 sent_pages 给出部分送达明细。"""
    _clear_notify_env(monkeypatch)
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "400")
    from types import SimpleNamespace

    calls: list[str] = []

    def flaky(title, text, *, category="report", **kw):
        calls.append(text)
        return [SimpleNamespace(ok=len(calls) != 2, skipped=False, channel="webhook")]

    res = run_watchlist_digest(
        symbols=[f"{i:06d}.SH" for i in range(20)],
        analyze_fn=lambda s, a: make_report(s),
        notify_fn=flaky,
    )
    assert res["pages"] > 2
    assert res["sent_pages"] == res["pages"] - 1
    assert res["sent"] is False  # 「部分送达」不冒充「全部送达」


def test_digest_oversized_single_block_flags_truncation(monkeypatch):
    """单只报告就超一页：显式 truncated + 正文头披露，绝不静默。"""
    _clear_notify_env(monkeypatch)
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "120")
    spy = NotifySpy()
    res = run_watchlist_digest(
        symbols=["600519.SH"], analyze_fn=lambda s, a: make_report(s), notify_fn=spy
    )
    assert res["truncated"] is True
    assert res["omitted_symbols"] == ["600519.SH"]
    assert "超长被截断" in spy.calls[0]["text"]
    assert len(spy.calls[0]["text"]) <= 120


def test_digest_limit_zero_means_notify_truncation_disabled(monkeypatch):
    """LQ_NOTIFY_TEXT_LIMIT=0 在 notify 层是「关闭兜底截断」，
    日报不能把它当成「上限 0」而一条都不发（回归边界）。"""
    _clear_notify_env(monkeypatch)
    monkeypatch.setenv("LQ_NOTIFY_TEXT_LIMIT", "0")
    spy = NotifySpy()
    res = run_watchlist_digest(
        symbols=["600519.SH"], analyze_fn=lambda s, a: make_report(s), notify_fn=spy
    )
    assert res["sent"] is True and res["pages"] == 1
    assert res["truncated"] is False
    assert "【600519.SH】" in spy.calls[0]["text"]
