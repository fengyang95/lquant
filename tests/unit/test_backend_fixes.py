"""后端缺陷修复专项回归：flusher 缓冲上限 / record 线程双检 / upsert 保留运行轨迹 /
北向资金主接口解析 / collect 日期 422 / 取消目标淘汰保护 / 北交所后缀 / ws RuntimeError。
"""
from __future__ import annotations

from datetime import date

import pytest

# ---------- flusher pending 上限 ----------

def test_flusher_pending_cap_drops_oldest():
    from lquant.monitor import flusher

    with flusher._PENDING_LOCK:
        flusher._PENDING_API.clear()
        dropped = flusher._extend_capped(flusher._PENDING_API, [1, 2, 3])
    assert dropped == 0
    with flusher._PENDING_LOCK:
        flusher._PENDING_API.clear()
        # cap=100000：塞 2 条占位 + 99999 新条 → 丢最旧 1 条
        flusher._PENDING_API.extend([0, 0])
        dropped = flusher._extend_capped(
            flusher._PENDING_API, list(range(99999)))
    assert dropped == 1
    assert len(flusher._PENDING_API) == flusher._PENDING_CAP
    assert flusher._PENDING_API[0] == 0  # 丢弃 1 条最旧后恰好回到上限
    with flusher._PENDING_LOCK:
        flusher._PENDING_API.clear()


# ---------- jobs._register_cancelable 淘汰保护 ----------

def test_register_cancelable_evicts_canceled_first():
    from lquant.server import jobs

    class LJ:
        _canceled = False

    jobs._CANCELABLE.clear()
    try:
        # 塞满：一个未取消运行中的旧任务 + 一批已取消旧任务
        running = jobs._CancelTarget(queue="q", local_job=LJ(), canceled=False)
        jobs._CANCELABLE["keep-running"] = running
        for i in range(jobs._CANCELABLE_MAX):
            done = jobs._CancelTarget(queue="q", local_job=None, canceled=True)
            jobs._CANCELABLE[f"old-{i}"] = done
        new = jobs._CancelTarget(queue="q", local_job=None, canceled=False)
        jobs._register_cancelable("new-job", new)
        # 运行中的未被挤掉
        assert jobs._CANCELABLE.get("keep-running") is running
        assert "new-job" in jobs._CANCELABLE
    finally:
        jobs._CANCELABLE.clear()


# ---------- sync.upsert_job 保留 last_run_* ----------

@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """LQ_ROOT + cwd 隔离（同 test_backfill_pool 模式）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_upsert_job_preserves_last_run(fake_settings):
    from datetime import datetime as dt

    from lquant.core.db import writer
    from lquant.sync import manager

    with writer() as con:
        con.execute("CREATE TABLE IF NOT EXISTS sync_job ("
                    "sync_id VARCHAR PRIMARY KEY, name VARCHAR, kind VARCHAR, "
                    "schedule_time VARCHAR, weekdays VARCHAR, params JSON, "
                    "enabled BOOLEAN DEFAULT TRUE, last_run_at TIMESTAMP, "
                    "last_status VARCHAR, last_rows INTEGER, "
                    "created_at TIMESTAMP, updated_at TIMESTAMP)")
    manager.upsert_job("j1", "测试", "collect", "17:30")
    with writer() as con:
        con.execute("UPDATE sync_job SET last_run_at = ?, last_status = ?, "
                    "last_rows = 7 WHERE sync_id = 'j1'",
                    [dt(2026, 9, 17, 17, 30), "ok"])
    # 同调度时间编辑（改开关等）：保留运行轨迹
    manager.upsert_job("j1", "测试", "collect", "17:30")
    with writer() as con:
        row = con.execute("SELECT last_run_at, last_status, last_rows "
                          "FROM sync_job WHERE sync_id = 'j1'").fetchone()
    assert row[0] == dt(2026, 9, 17, 17, 30)
    assert row[1] == "ok"
    assert row[2] == 7
    # 调度时间变化：旧轨迹对应旧调度，必须重置（否则当天不再按新时间跑）
    manager.upsert_job("j1", "测试", "collect", "18:00")
    with writer() as con:
        row2 = con.execute("SELECT last_run_at, last_status, last_rows "
                           "FROM sync_job WHERE sync_id = 'j1'").fetchone()
    assert row2[0] is None and row2[1] is None and row2[2] is None


# ---------- 北向资金 ----------

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_northbound_no_longer_reads_kamt_net_inflow(monkeypatch):
    """回归：北向已换到数据中心报表，不再读 kamt 的「净买额」。

    旧实现解析 kamt/get 的净买额，源站停发后它把 0.0 当真值写库；
    这条测试钉住「不再调 kamt」这件事，防止有人改回去。
    """
    from lquant.market.collectors import northbound as nb

    urls: list[str] = []

    def fake_em_get(url, **kw):
        urls.append(url)
        return _Resp({"success": True, "result": {"data": [
            {"TRADE_DATE": "2026-09-17 00:00:00", "MUTUAL_TYPE": t,
             "DEAL_AMT": amt, "DEAL_NUM": 5, "NET_DEAL_AMT": None}
            for t, amt in (("001", 3000.0), ("003", 2000.0), ("005", 5000.0))
        ]}})

    monkeypatch.setattr(nb, "em_get", fake_em_get)
    df = nb.fetch_northbound(date(2026, 9, 17))
    assert all("kamt" not in u for u in urls)
    assert "RPT_MUTUAL_DEAL_HISTORY" in urls[0]
    # 停发后净买额为 NULL（不是 0）
    assert df["total_net_inflow"][0] is None
    assert df["net_published"][0] is False


def test_northbound_all_empty_raises(monkeypatch):
    from lquant.core.errors import DataUnavailable
    from lquant.market.collectors import northbound as nb

    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp({"data": None}))
    with pytest.raises(DataUnavailable):
        nb.fetch_northbound(date(2026, 9, 17))


# ---------- collect 非法日期 → 422 ----------

def test_collect_bad_date_422(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lquant.server.api import market

    monkeypatch.setattr(
        "lquant.market.scheduler.collect_and_save",
        lambda **kw: {"status": "ok"})
    app = FastAPI()
    app.include_router(market.router)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/market/collect", json={"trade_date": "not-a-date"})
    assert r.status_code == 422
    r2 = c.post("/market/collect", json={"trade_date": "2026-09-17"})
    assert r2.status_code == 200


# ---------- data_admin 北交所后缀 ----------

def test_bare_code_bj_suffix():
    from lquant.server.api.data_admin import bare_code_or_full

    assert bare_code_or_full("830799") == "830799.BJ"
    assert bare_code_or_full("430047") == "430047.BJ"
    assert bare_code_or_full("600519") == "600519.SH"
    assert bare_code_or_full("000001") == "000001.SZ"
    assert bare_code_or_full("600519.SH") == "600519.SH"
