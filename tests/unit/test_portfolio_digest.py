"""组合绩效日报 portfolio_digest：快照数值、报文格式、编排失败语义。

隔离方式：LQ_PAPER_DB 指向 tmp（paper/store.db_path() 每次读 env，
无缓存）。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

ACCOUNT = "demo"


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_PAPER_DB", str(tmp_path / "paper.db"))
    yield tmp_path


@pytest.fixture(autouse=True)
def _isolate_paper_db(tmp_path, monkeypatch):
    """对账告警的幂等状态挂在 paper 库（LQ_PAPER_DB）：既换独立的库文件，
    又显式清一次幂等表 —— 即使某些用例共用库，也不会「整文件跑 FAIL、
    单跑 PASS」。用例顺序因此无关。"""
    monkeypatch.setenv("LQ_PAPER_DB", str(tmp_path / "paper_autouse.db"))
    from lquant.paper.service import reset_reconcile_alert_state

    reset_reconcile_alert_state()
    yield


def _seed_account(name: str = ACCOUNT):
    """账户 + 两只持仓 + 三天官方净值（峰值在第 2 天，回撤/负盈亏可测）。"""
    from lquant.paper import store as paper_store
    from lquant.paper.engine import PaperBroker, PaperConfig, PaperPosition

    paper_store.create_account(name, 200000.0, strategy="test")
    broker = PaperBroker(PaperConfig(initial_cash=200000.0))
    broker.cash = 13000.0
    broker.positions["600519"] = PaperPosition(
        symbol="600519", qty=100, available=100, avg_cost=1400.0, last_price=1500.0, name="贵州茅台"
    )  # 市值 150,000
    broker.positions["510300"] = PaperPosition(
        symbol="510300", qty=1000, available=1000, avg_cost=35.0, last_price=37.0, name="沪深300ETF"
    )  # 市值 37,000
    paper_store.save_broker(name, broker)

    # official 净值：198,000 → 峰值 205,000 → 200,000（对账口径：
    # nav = cash + Σ(qty × 收盘价)，此处按叙事给数）
    paper_store.record_nav(name, date(2024, 1, 3), 198000.0, 13000.0, 2, "official")
    paper_store.record_nav(name, date(2024, 1, 4), 205000.0, 13000.0, 2, "official")
    paper_store.record_nav(name, date(2024, 1, 5), 200000.0, 13000.0, 2, "official")


# ---------------- 快照 ----------------


def test_snapshot_official_nav_math(paper_env):
    _seed_account()
    from lquant.market.digest import portfolio_snapshot

    snap = portfolio_snapshot(ACCOUNT)
    assert snap["asof"] == "2024-01-05"
    assert snap["nav"] == pytest.approx(200000.0)
    assert snap["prev_nav"] == pytest.approx(205000.0)
    assert snap["peak"] == pytest.approx(205000.0)
    assert snap["day_pnl"] == pytest.approx(-5000.0)
    assert snap["day_pct"] == pytest.approx(-5000.0 / 205000.0)
    assert snap["drawdown"] == pytest.approx(1 - 200000.0 / 205000.0)
    assert snap["cash"] == pytest.approx(13000.0)
    assert snap["n_positions"] == 2
    # 权重分母 = 官方 nav：600519 150,000/200,000 = 0.75
    assert snap["top1_weight"] == pytest.approx(0.75)
    assert snap["top3_weight"] == pytest.approx(0.75 + 37000.0 / 200000.0)
    # positions 按市值降序，600519 在前
    assert snap["positions"][0]["symbol"] == "600519"
    assert snap["positions"][0]["weight"] == pytest.approx(0.75)


def test_snapshot_falls_back_to_cash_plus_value(paper_env):
    """无净值记录：分母回退 cash + Σ市值，权重仍可算。"""
    _seed_account()
    # 清掉净值：直接连 sqlite 删行（record_nav 只有 upsert）
    import sqlite3

    from lquant.paper import store as paper_store
    from lquant.paper.store import db_path

    con = sqlite3.connect(db_path())
    con.execute("DELETE FROM paper_nav")
    con.commit()
    con.close()

    from lquant.market.digest import portfolio_snapshot

    snap = portfolio_snapshot(ACCOUNT)
    assert snap["nav"] is None and snap["asof"] is None
    assert snap["day_pnl"] is None and snap["drawdown"] is None
    assert snap["top1_weight"] == pytest.approx(150000.0 / 200000.0)
    paper_store.get_account(ACCOUNT)  # 账户本身无恙


def test_snapshot_missing_account_raises(paper_env):
    from lquant.market.digest import portfolio_snapshot
    from lquant.paper.store import AccountNotFound

    with pytest.raises(AccountNotFound):
        portfolio_snapshot("ghost")


def test_snapshot_unpriced_position_weight_is_none(paper_env):
    """last_price=0（未定价新持仓）：权重「—」而非伪装成 0% 集中度。"""
    from lquant.paper import store as paper_store
    from lquant.paper.engine import PaperPosition

    _seed_account()
    broker = paper_store.load_broker(ACCOUNT)
    broker.positions["301999"] = PaperPosition(
        symbol="301999", qty=500, available=500, avg_cost=20.0, last_price=0.0, name="未定价新股"
    )
    paper_store.save_broker(ACCOUNT, broker)

    from lquant.market.digest import portfolio_snapshot

    snap = portfolio_snapshot(ACCOUNT)
    unpriced = next(p for p in snap["positions"] if p["symbol"] == "301999")
    assert unpriced["weight"] is None
    # TOP1 不被未定价持仓稀释：仍是 600519 的 0.75
    assert snap["top1_weight"] == pytest.approx(0.75)
    assert snap["n_positions"] == 3


def test_snapshot_all_unpriced_aggregate_is_unknown_not_zero(paper_env):
    """全部持仓从未定价：聚合 TOP1/TOP3 是「未知」（None），不是 0% 集中度。

    旧实现 `weights or 0.0` 让「算不出」伪装成「没有集中度」，
    TOP1/TOP3 风控信号失真。
    """
    from lquant.paper import store as paper_store
    from lquant.paper.engine import PaperBroker, PaperConfig, PaperPosition

    paper_store.create_account("unpriced", 100000.0, strategy="test")
    broker = PaperBroker(PaperConfig(initial_cash=100000.0))
    broker.cash = 1000.0
    broker.positions["301999"] = PaperPosition(
        symbol="301999", qty=500, available=500, avg_cost=20.0, last_price=0.0, name="未定价新股"
    )
    paper_store.save_broker("unpriced", broker)

    from lquant.market.digest import format_portfolio_report, portfolio_snapshot

    snap = portfolio_snapshot("unpriced")
    assert snap["n_positions"] == 1
    assert snap["top1_weight"] is None and snap["top3_weight"] is None
    assert snap["positions"][0]["weight"] is None
    assert "TOP1 —" in format_portfolio_report(snap)


def test_snapshot_dirty_nav_derived_fields_none(paper_env):
    """nav<=0 的对账事故：派生比例置 None（「—」优于荒谬数）。"""
    _seed_account()
    import sqlite3

    from lquant.paper.store import db_path

    con = sqlite3.connect(db_path())
    con.execute("UPDATE paper_nav SET nav = -100.0 WHERE trade_date = '2024-01-05'")
    con.commit()
    con.close()

    from lquant.market.digest import format_portfolio_report, portfolio_snapshot

    snap = portfolio_snapshot(ACCOUNT)
    assert snap["nav"] == pytest.approx(-100.0)  # 原始值照传（不静默）
    assert snap["day_pct"] is None  # 负/负 → 不再出 +100%
    assert snap["drawdown"] is None
    assert snap["day_pnl"] == pytest.approx(-100.0 - 205000.0)  # 差值仍如实
    # nav<=0 是脏分母：个股权重与聚合集中度都必须是「未知」，不能出负百分比
    assert snap["positions"][0]["weight"] is None
    assert snap["top1_weight"] is None and snap["top3_weight"] is None
    assert "TOP1 —" in format_portfolio_report(snap)


# ---------------- 报文 ----------------


def test_format_report_contains_key_lines(paper_env):
    _seed_account()
    from lquant.market.digest import format_portfolio_report, portfolio_snapshot

    text = format_portfolio_report(portfolio_snapshot(ACCOUNT))
    assert "【组合日报 · demo】2024-01-05 总资产 200,000.00 元" in text
    assert "当日盈亏 -5,000.00 元（-2.4%）" in text
    assert "当前回撤 2.4%（峰值 205,000.00 元）" in text
    assert "现金 13,000.00 元 · 持仓 2 只" in text
    assert "TOP1 75.0%" in text
    assert "贵州茅台 100股 市值150,000.00（75.0%）" in text


def test_format_report_defensive_on_empty(paper_env):
    """无净值无持仓：显示「—」不炸（防御取值契约）。"""
    from lquant.paper import store as paper_store

    paper_store.create_account("empty", 100000.0)
    from lquant.market.digest import format_portfolio_report, portfolio_snapshot

    text = format_portfolio_report(portfolio_snapshot("empty"))
    assert "无净值记录" in text
    assert "当日盈亏" not in text  # 无净值 → 盈亏/回撤行整体不出现
    assert "当前回撤" not in text
    assert "持仓 0 只" in text
    assert "TOP1 0.0%" in text  # 无持仓 → 集中度为 0 而非缺失


# ---------------- 对账告警接线（paper.service._notify_reconcile） ----------------
# reconcile 的字典契约「无 intraday 时 verdict 仍为 ok」已被
# tests/unit/test_metrics_paper_extra.py 固化（属另一层语义，未动）；告警
# 判据在 service 侧补齐，这里锁住「没有基准 / 全部持仓无官方价 → 必须告警」。


def _reconcile_report(**over):
    rep = {
        "account": "demo",
        "trade_date": "2026-09-17",
        "nav_official": 100000.0,
        "nav_intraday": 100000.0,
        "rel_dev": 0.0,
        "stale_symbols": [],
        "n_held": 1,
        "n_uncovered": 0,
        "verdict": "ok",
        "detail": "",
    }
    rep.update(over)
    return rep


def _spy_reconcile_notify(monkeypatch):
    fired: list[dict] = []

    def spy(title, text, **kw):
        fired.append({"title": title, "text": text, **kw})
        return [SimpleNamespace(ok=True, channel="wecom")]

    monkeypatch.setattr("lquant.notify.notify", spy)
    return fired


def test_reconcile_notify_silent_on_normal_ok(monkeypatch):
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile("demo", "2026-09-17", _reconcile_report())
    assert fired == []  # 有基准且偏差在容忍度内：静默，不刷屏


def test_reconcile_notify_fires_when_no_intraday_baseline(monkeypatch):
    """(a) 没有盘中基准：rel_dev/detail 全空，但必须告警，不能静音。"""
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile("demo", "2026-09-17", _reconcile_report(nav_intraday=None))
    assert len(fired) == 1
    assert fired[0]["category"] == "alert" and fired[0]["severity"] == "warning"
    assert "无盘中基准" in fired[0]["text"]
    assert "unverified" in fired[0]["text"]  # 不显示自相矛盾的 verdict=ok


def test_reconcile_notify_fires_when_all_positions_stale(monkeypatch):
    """(b) 全部持仓取不到官方收盘价：官方 NAV 退回盯市价，偏差≈0 是假阴性。"""
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile(
        "demo",
        "2026-09-17",
        _reconcile_report(stale_symbols=["600519"], n_uncovered=1, n_held=1),
    )
    assert len(fired) == 1
    assert "全部 1 只持仓" in fired[0]["text"]


def test_reconcile_notify_partial_stale_alone_is_not_noise(monkeypatch):
    """部分 stale（个别停牌）属常态：单独不告警，避免把告警灌成噪音。"""
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile(
        "demo",
        "2026-09-17",
        _reconcile_report(stale_symbols=["600519"], n_uncovered=1, n_held=3),
    )
    assert fired == []


def test_reconcile_notify_body_carries_nav_evidence(monkeypatch):
    """告警正文必须带官方/盘中净值、偏差与 stale/未覆盖数量。

    回归：旧正文只有 trade_date+verdict+detail，收到告警的人看不到这两个
    净值数字，得回看板上翻才能判断要不要立刻介入。
    """
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile(
        "demo",
        "2026-09-17",
        _reconcile_report(
            verdict="critical",
            nav_official=100000.0,
            nav_intraday=94877.0,
            rel_dev=0.05123,
            stale_symbols=["600519"],
            n_uncovered=1,
            n_held=3,
            detail="偏差 5.123% 严重超阈",
        ),
    )
    text = fired[0]["text"]
    assert "nav_official=100000.0" in text
    assert "nav_intraday=94877.0" in text
    assert "rel_dev=0.05123" in text
    assert "stale=1" in text and "n_uncovered=1" in text


def test_reconcile_notify_same_day_verdict_only_once(monkeypatch):
    """同账户同日同 verdict 只发一次：``lq paper close`` 重跑不再重复告警。"""
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    rep = _reconcile_report(verdict="critical", nav_intraday=94877.0, rel_dev=0.05123)
    _notify_reconcile("demo", "2026-09-17", rep)
    _notify_reconcile("demo", "2026-09-17", rep)
    assert len(fired) == 1


def test_reconcile_notify_distinct_verdict_or_date_not_suppressed(monkeypatch):
    """幂等键含 verdict 与 trade_date：不同 verdict / 不同日都是新告警，都要发。"""
    from lquant.paper.service import _notify_reconcile

    fired = _spy_reconcile_notify(monkeypatch)
    _notify_reconcile(
        "demo", "2026-09-17", _reconcile_report(verdict="warning", rel_dev=0.02,
                                                 nav_intraday=98000.0)
    )
    _notify_reconcile(
        "demo", "2026-09-17", _reconcile_report(verdict="critical", rel_dev=0.05123,
                                                 nav_intraday=94877.0)
    )
    # 同账户同 verdict 但换一天：也应视为新告警
    _notify_reconcile(
        "demo", "2026-09-18", _reconcile_report(verdict="critical", rel_dev=0.05123,
                                                 nav_intraday=94877.0)
    )
    assert len(fired) == 3


def test_reconcile_notify_failed_send_allows_retry(monkeypatch):
    """通道全挂（ok=False）不登记为已完成：下一次 close 仍会重试告警。"""
    from lquant.paper.service import _notify_reconcile

    calls: list[str] = []

    def failing(title, text, **kw):
        calls.append(text)
        return [SimpleNamespace(ok=False, channel="webhook")]

    monkeypatch.setattr("lquant.notify.notify", failing)
    rep = _reconcile_report(verdict="critical", nav_intraday=94877.0, rel_dev=0.05123)
    _notify_reconcile("demo", "2026-09-17", rep)
    _notify_reconcile("demo", "2026-09-17", rep)
    assert len(calls) == 2  # 没送出去就不占用幂等键


def test_day_close_non_trading_day_skips_nav_write(paper_env, monkeypatch):
    """非交易日 day_close 不落 official 净值，返回值显式 skipped。

    回归：reconcile 曾无条件 record_nav —— 周末重跑 close 会写进一个日历上
    不存在的 official 点，组合日报按 trade_date 取最后一根当「当前」，
    prev_nav / day_pnl / peak / 回撤全部以幽灵点为基准。
    """
    from lquant.paper import service, store

    store.create_account("demo", 100000.0)
    monkeypatch.setattr(service, "_is_trading_day", lambda d: False)
    out = service.day_close("demo", "2024-01-06")
    assert out["skipped"] is True
    assert out["skip_reason"] == "non_trading_day"
    assert out["reconcile"] is None
    assert len(store.nav_frame("demo")) == 0  # 没有幽灵点落库


def test_day_close_non_trading_day_ghost_account_still_raises(monkeypatch):
    """跳过不等于放行：非交易日跑不存在的账户仍要显式 AccountNotFound。"""
    from lquant.paper import service, store

    monkeypatch.setattr(service, "_is_trading_day", lambda d: False)
    with pytest.raises(store.AccountNotFound):
        service.day_close("ghost", "2024-01-06")


# ---------------- 编排 ----------------


def _spy_notify():
    calls = []

    def spy(title, text, **kw):
        calls.append({"title": title, "text": text, **kw})

    return spy, calls


def test_run_digest_happy_path(paper_env):
    _seed_account()
    from lquant.market.digest import run_portfolio_digest

    spy, calls = _spy_notify()
    res = run_portfolio_digest(account=ACCOUNT, notify_fn=spy)
    assert res["sent"] is True and res["skipped"] is None
    assert res["snapshot"]["nav"] == pytest.approx(200000.0)
    assert len(calls) == 1
    assert calls[0]["category"] == "report"
    assert calls[0]["title"] == "组合日报 · demo"


def test_run_digest_missing_account_skipped(paper_env):
    from lquant.market.digest import run_portfolio_digest

    spy, calls = _spy_notify()
    res = run_portfolio_digest(account="ghost", notify_fn=spy)
    assert res["sent"] is False
    assert res["skipped"] == "账户不存在: ghost"
    assert calls == []  # 不发送空报告


def test_run_digest_snapshot_failure_goes_error_channel(paper_env, monkeypatch):
    """快照构建失败：不吞错，改走 error 通道报错摘要。"""
    import lquant.market.digest as digest_mod

    def boom(account):
        raise RuntimeError("sqlite disk io error")

    monkeypatch.setattr(digest_mod, "portfolio_snapshot", boom)
    spy, calls = _spy_notify()
    res = digest_mod.run_portfolio_digest(account=ACCOUNT, notify_fn=spy)
    assert res["sent"] is True  # error 摘要真实送达
    assert "sqlite disk io error" in res["error"]
    assert calls[0]["category"] == "error"
    assert "正文" in calls[0]["text"]


def test_run_digest_sent_reflects_delivery(paper_env):
    """通道全挂（SendResult.ok=False）：sent 如实回 False，不冒充已送达。"""
    _seed_account()
    from lquant.market.digest import run_portfolio_digest

    def failing_notify(title, text, **kw):
        return [SimpleNamespace(ok=False, channel="webhook")]

    res = run_portfolio_digest(account=ACCOUNT, notify_fn=failing_notify)
    assert res["sent"] is False
