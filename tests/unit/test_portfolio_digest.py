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


def test_snapshot_dirty_nav_derived_fields_none(paper_env):
    """nav<=0 的对账事故：派生比例置 None（「—」优于荒谬数）。"""
    _seed_account()
    import sqlite3

    from lquant.paper.store import db_path

    con = sqlite3.connect(db_path())
    con.execute("UPDATE paper_nav SET nav = -100.0 WHERE trade_date = '2024-01-05'")
    con.commit()
    con.close()

    from lquant.market.digest import portfolio_snapshot

    snap = portfolio_snapshot(ACCOUNT)
    assert snap["nav"] == pytest.approx(-100.0)  # 原始值照传（不静默）
    assert snap["day_pct"] is None  # 负/负 → 不再出 +100%
    assert snap["drawdown"] is None
    assert snap["day_pnl"] == pytest.approx(-100.0 - 205000.0)  # 差值仍如实


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
