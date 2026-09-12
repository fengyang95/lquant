"""API 层集成测试 —— generate_demo 生成自包含合成环境，全链路离线跑。

不依赖任何本地真实数据（data/duckdb、data/parquet 均不需要）：
tmp 目录 + generate_demo 地基 + demo 采集灌看板表，任何机器/CI 皆可跑。

覆盖：health / data(coverage/securities/daily/indicators/quote) / watchlist CRUD /
factors 注册-列表-详情-校验 / backtests run-list-detail-compare / market collect 健康度 /
strategies 枚举 / ws 任务推送。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# 测试环境不启动同步后台线程（避免测试期间触发真实采集）
os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


def _ensure_cwd():
    """cwd 指向的目录被删（pytest tmp 清理）时，os.getcwd() 会炸 —— 先兜底恢复。"""
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir(os.path.expanduser("~"))


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    """chdir 到 tmp 目录，用 generate_demo 造一份自包含合成数据环境。"""
    _ensure_cwd()
    base = tmp_path_factory.mktemp("api")
    _old = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns
    from lquant.market.scheduler import collect_and_save
    from lquant.market.schema import ensure_market_tables
    with writer() as con:                # startup 前手动建库+看板表
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
        ensure_market_tables(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    # 市场看板表灌一轮 demo 采集 —— breadth/snapshot/index 端点
    # 不再依赖测试文件内的执行顺序（此前靠真实库里的存量数据）
    collect_and_save(schedule=None, demo=True)
    yield base
    os.chdir(_old)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:     # with 触发 startup DDL/迁移
        yield c


# ---------- 基础 ----------

def test_health(client):
    r = client.get("/api/health/ping")
    assert r.status_code == 200


def test_strategies_endpoint(client):
    """策略注册表枚举（扩展点：自定义策略注册后自动出现在这里）。"""
    r = client.get("/api/strategies")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "factor_topn" in names
    assert any(s.get("label") for s in r.json())


# ---------- data ----------

def test_data_coverage(client):
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    body = r.json()
    assert "daily_lake" in body and "tables" in body
    assert body["daily_lake"]["rows"] > 0            # 拷贝来的 demo 湖非空


def test_data_securities_and_daily(client):
    r = client.get("/api/data/securities", params={"q": "600519", "limit": 3})
    assert r.status_code == 200
    hits = r.json()
    assert hits and hits[0]["symbol"].startswith("600519")
    sym = hits[0]["symbol"]
    r2 = client.get("/api/data/daily", params={"symbol": sym, "limit": 5})
    assert r2.status_code == 200
    rows = r2.json()
    assert 0 < len(rows) <= 5
    assert {"trade_date", "open", "close"} <= set(rows[0])


def test_data_indicators(client):
    r = client.get("/api/data/indicators", params={"symbol": "600519.SH", "limit": 30})
    assert r.status_code == 200
    rows = r.json()
    assert rows and "rsi14" in rows[-1]


def test_data_quote_graceful(client):
    """实时行情：有网返回真数据，无网/沙箱必须优雅降级而不是 500。"""
    r = client.get("/api/data/quote", params={"symbol": "600519"})
    assert r.status_code == 200
    assert "available" in r.json()


# ---------- watchlist ----------

def test_watchlist_crud_roundtrip(client):
    client.delete("/api/watchlist/600519.SH")            # 拷贝库可能已含该票，先清
    r = client.post("/api/watchlist", json={"symbol": "600519", "note": "测试"})
    assert r.status_code == 200
    lst = client.get("/api/watchlist").json()
    assert any(w["symbol"] == "600519.SH" and w.get("name") for w in lst)
    assert client.delete("/api/watchlist/600519.SH").status_code == 200
    assert client.delete("/api/watchlist/600519.SH").status_code == 404


def test_watchlist_invalid_symbol_422(client):
    assert client.post("/api/watchlist", json={"symbol": "abc"}).status_code == 422


# ---------- factors ----------

def test_factor_register_list_detail_validation(client):
    assert client.post("/api/factors", json={
        "name": "tst_mom5", "expression": "Rank(Ts_Mean($close,5)/$close-1)",
    }).status_code == 200
    names = [f["name"] for f in client.get("/api/factors").json()]
    assert "tst_mom5" in names
    detail = client.get("/api/factors/tst_mom5")
    assert detail.status_code == 200
    assert detail.json()["expression"].startswith("Rank(")
    # 分页参数生效
    paged = client.get("/api/factors", params={"limit": 1, "offset": 0})
    assert len(paged.json()) == 1


def test_factor_bad_dsl_422(client):
    r = client.post("/api/factors", json={"name": "tst_bad", "expression": "Rank(Ts_Foo($close,5))"})
    assert r.status_code == 422


def test_factor_detail_404(client):
    assert client.get("/api/factors/no_such_factor").status_code == 404


def test_builtin_factors_list_and_filter(client):
    r = client.get("/api/factors/builtin")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 158                       # Qlib Alpha158 全量
    assert {"kbar", "price"} <= {x["family"] for x in items}
    r2 = client.get("/api/factors/builtin", params={"family": "ma"})
    assert {x["name"] for x in r2.json()} == {f"MA{d}" for d in (5, 10, 20, 30, 60)}
    r3 = client.get("/api/factors/builtin", params={"q": "RSV"})
    assert {x["name"] for x in r3.json()} == {f"RSV{d}" for d in (5, 10, 20, 30, 60)}


def test_seed_builtin_and_evaluate(client):
    r = client.post("/api/factors/seed-builtin",
                    json={"names": ["MA20", "RSV10"]})
    assert r.status_code == 200
    assert r.json()["seeded"] == 2
    names = [f["name"] for f in client.get("/api/factors").json()]
    assert {"MA20", "RSV10"} <= set(names)
    # 内置因子走完整评价管线
    ev = client.post("/api/factors/evaluate", json={
        "factor": "MA20", "formula": "MA20", "start": "2024-06-01"})
    assert ev.status_code == 200
    body = ev.json()
    assert "ic" in body and body["n_samples"] > 0
    assert body["report_url"].startswith("/api/factors/reports/")
    assert client.get(body["report_url"]).status_code == 200


def test_evaluate_series_chart_payload(client):
    """评价图表数据包：IC 序列 / 分层净值 / 分组柱 / 衰减 / 分年度。"""
    r = client.post("/api/factors/evaluate/series", json={
        "factor": "MA20", "formula": "MA20", "n_groups": 5, "start": "2024-06-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["ic"]["dates"] and len(body["ic"]["dates"]) == len(body["ic"]["ic"])
    assert len(body["ic"]["cum_ic"]) == len(body["ic"]["ic"])
    q = body["quantile"]
    assert set(q["curves"]) >= {f"q{i}" for i in range(1, 6)} | {"long_short"}
    assert len(q["dates"]) == len(q["curves"]["q1"])
    assert len(q["groups"]) == 5
    assert body["decay"]["horizons"] == [1, 5, 10, 20]
    assert all(v is None or isinstance(v, (int, float)) for v in body["decay"]["ic"])
    assert isinstance(body["ic_by_year"], list)
    # 滚动窗口序列：窗口数与序列长度一致，字段齐全
    rw = body["rolling"]
    assert rw["window"] == 60
    assert len(rw["dates"]) == len(rw["ic"]) == len(rw["rank_ic"]) == len(rw["ir"])
    assert len(rw["dates"]) > 0


def test_evaluate_series_rolling_window_override(client):
    """window 参数透传：window=20 的滚动序列比 window=60 的更长。"""
    def _roll_len(window: int) -> int:
        r = client.post("/api/factors/evaluate/series", json={
            "factor": "MA20", "formula": "MA20", "start": "2024-06-01",
            "window": window})
        assert r.status_code == 200
        return len(r.json()["rolling"]["dates"])

    assert _roll_len(20) > _roll_len(60)


def test_evaluate_excess_topn_style_payload(client):
    """研报三件套：超额体系 / Top-N 收缩 / 中性化后风格相关，字段齐全且形态正确。"""
    r = client.post("/api/factors/evaluate/series", json={
        "factor": "MA20", "formula": "MA20", "n_groups": 5, "start": "2024-06-01",
        "top_ns": [20, 50]})
    assert r.status_code == 200
    body = r.json()
    # 超额净值曲线：日期对齐 + 各组 ex_* + 多空相对强弱
    ex = body["excess"]
    assert ex["dates"] and set(ex["curves"]) >= {f"ex_q{i}" for i in range(1, 6)} | {"ex_long_short"}
    assert len(ex["dates"]) == len(ex["curves"]["ex_q1"])
    # Top-N：请求的每个 N 都有行，指标形态正确
    tn = {row["n"]: row for row in body["top_n"]}
    assert set(tn) == {20, 50}
    for row in tn.values():
        for k in ("annual_return", "annual_excess", "excess_sharpe",
                  "max_drawdown", "annual_turnover"):
            assert row[k] is None or isinstance(row[k], (int, float))
    # 风格相关：styles 列表 + 阈值判定字段
    sc = body["style_corr"]
    assert sc["threshold"] == 0.14
    assert isinstance(sc["styles"], list)
    assert sc["passed"] in (True, False, None)


# ---------- backtests ----------

def test_backtest_run_list_detail_compare(client):
    r1 = client.post("/api/backtests/run", json={
        "formula": "pct_change_20", "top_n": 3, "start": "2026-01-01"})
    r2 = client.post("/api/backtests/run", json={
        "formula": "pct_change_5", "top_n": 2, "start": "2026-01-01"})
    assert r1.status_code == 200 and r2.status_code == 200
    a, b = r1.json()["run_id"], r2.json()["run_id"]

    lst = client.get("/api/backtests").json()
    assert {a, b} <= {x["run_id"] for x in lst}

    detail = client.get(f"/api/backtests/{a}")
    assert detail.status_code == 200

    cmp_ = client.get("/api/backtests/compare", params={"ids": f"{a},{b}"})
    assert cmp_.status_code == 200
    body = cmp_.json()
    assert len(body["series"]) == 2 and len(body["dates"]) > 10

    # 参数校验
    assert client.get("/api/backtests/compare", params={"ids": a}).status_code == 422
    assert client.get("/api/backtests/compare",
                      params={"ids": f"{a},ghost"}).status_code == 404


def test_jq_run_code_attribution_holdings(client):
    """聚宽代码回测全链路：run-code → code → holdings → attribution。"""
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    order_target_value("600519.SH", context.portfolio.total_value * 0.5)
    order_target_value("000001.SZ", context.portfolio.total_value * 0.3)
'''
    r = client.post("/api/backtests/run-code", json={
        "code": code, "start": "2026-01-01", "initial_cash": 1_000_000})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert r.json()["n_nav_points"] > 10

    # 策略代码可取回
    c = client.get(f"/api/backtests/{rid}/code").json()
    assert "order_target_value" in c["code"]

    # 每日持仓：日期索引 + 单日明细
    h = client.get(f"/api/backtests/{rid}/holdings").json()
    assert len(h["dates"]) > 10
    last_day = h["dates"][-1]["date"]
    hd = client.get(f"/api/backtests/{rid}/holdings", params={"day": last_day}).json()
    assert hd["positions"] and sum(p["weight"] for p in hd["positions"]) <= 1.0

    # 归因：个股贡献 + Brinson + 风险指标
    att = client.get(f"/api/backtests/{rid}/attribution").json()
    assert att["stock_contribution"]["n_stocks"] >= 1
    assert att["brinson"]["groups"]
    assert "alpha_annual" in att["risk"]

    # 语法错误 → 422 带信息
    bad = client.post("/api/backtests/run-code", json={
        "code": "def initialize(context\n  pass", "start": "2026-01-01"})
    assert bad.status_code == 422


# ---------- sync：定时同步 ----------

def test_sync_jobs_crud_and_run(client):
    lst = client.get("/api/sync/jobs").json()
    assert {"close", "evening", "daily", "adj"} <= {j["sync_id"] for j in lst}

    r = client.post("/api/sync/jobs", json={
        "sync_id": "api_test", "name": "API 测试", "kind": "collect",
        "schedule_time": "12:00", "weekdays": "5", "params": {}})
    assert r.status_code == 200

    # 立即执行（demo 离线）
    run = client.post("/api/sync/run", json={"sync_id": "close", "demo": True})
    assert run.status_code == 200
    assert run.json()["status"] in ("ok", "partial")

    hist = client.get("/api/sync/history").json()
    assert any(h["sync_id"] == "close" for h in hist)

    tog = client.post("/api/sync/jobs/api_test/toggle", json={"enabled": False})
    assert tog.status_code == 200 and tog.json()["enabled"] is False
    assert client.delete("/api/sync/jobs/api_test").status_code == 200
    assert client.post("/api/sync/run", json={}).status_code == 422


def test_sync_coverage(client):
    cov = client.get("/api/sync/coverage").json()
    assert "lake" in cov and "reference" in cov and "market_tables" in cov
    assert cov["lake"]["rows"] > 0                     # 拷贝来的 demo 湖非空
    assert any(t["table"] == "security" for t in cov["reference"])
    assert any(t["table"] == "index_daily" for t in cov["market_tables"])


def test_market_index_endpoint(client):
    """指数端点：demo 采集一轮后应有数据。"""
    client.post("/api/market/collect", json={"demo": True})
    r = client.get("/api/market/index")
    assert r.status_code == 200
    body = r.json()
    if body:
        row = body[0]
        assert {"symbol", "name", "close", "chg", "dates", "closes"} <= set(row)


# ---------- market 健康度 ----------

def test_market_collect_and_status(client):
    r = client.post("/api/market/collect", json={"demo": True})
    assert r.status_code == 200
    status = client.get("/api/market/collect-status").json()
    assert "jobs" in status or "collectors" in status or isinstance(status, list)


def test_market_breadth(client):
    r = client.get("/api/market/breadth", params={"days": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] and body["latest"]["n"] > 0
    assert 0 <= body["latest"]["up_ratio"] <= 1
    assert len(body["history"]) <= 20
    assert {"trade_date", "up", "down", "med_chg"} <= set(body["history"][0])


def test_market_batch_aggregate(client):
    r = client.get("/api/market/batch",
                   params={"symbols": "600519,510300.SH,159915", "days": 30})
    assert r.status_code == 200
    body = r.json()
    assert len(body["series"]) == 3
    assert body["summary"]["n"] == 3
    assert len(body["equal_weight_nav"]) == len(body["dates"])
    assert body["equal_weight_nav"][0] == 1.0
    # 裸码 / 带后缀混用、超量
    assert client.get("/api/market/batch",
                      params={"symbols": ",".join(["600519"] * 21)}).status_code == 422


# ---------- 封套 / settings ----------

def test_settings_get_put_reset_enveloped(client):
    """封套 {code,data,message,trace_id} + 配置读写生效。"""
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"code", "data", "message", "trace_id"}
    items = {i["key"]: i for i in body["data"]}
    assert items["rebalance_default"]["value"] == "monthly"
    assert items["rebalance_default"]["source"] in ("default", "runtime")
    assert "providers_order" in items

    r2 = client.put("/api/settings/rebalance_default", json={"key": "rebalance_default",
                                                             "value": "weekly"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["code"] == 0 and r2.json()["data"]["value"] == "weekly"

    # 覆盖后 source 变 runtime
    after = {i["key"]: i["source"] for i in client.get("/api/settings").json()["data"]}
    assert after["rebalance_default"] == "runtime"

    # 非法枚举值 → 422 信封
    bad = client.put("/api/settings/price_mode_default",
                     json={"key": "price_mode_default", "value": "instant"})
    assert bad.status_code == 422 and bad.json()["code"] == 1

    # 未知 key → 422；key 不一致 → 422
    assert client.put("/api/settings/nope", json={"key": "nope",
                                                  "value": "x"}).status_code == 422
    assert client.put("/api/settings/rebalance_default",
                      json={"key": "other", "value": "x"}).status_code == 422

    # 重置回默认
    reset = client.delete("/api/settings/rebalance_default")
    assert reset.status_code == 200 and reset.json()["data"]["reset"] is True
    after2 = {i["key"]: i["value"] for i in client.get("/api/settings").json()["data"]}
    assert after2["rebalance_default"] == "monthly"


def test_settings_providers(client):
    r = client.get("/api/settings/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    names = [p["name"] for p in body["data"]]
    assert "baostock" in names and "mootdx" in names
    assert any(p["enabled"] for p in body["data"])


# ---------- ETF ----------

def _seed_etf_meta():
    """测试库写入 3 只 ETF 元数据（枚举竞价：互不相同、字段齐全）。"""
    from lquant.core.db import writer

    with writer() as con:
        con.execute("DELETE FROM etf_meta")
        rows = [
            ("510300.SH", "沪深300ETF", "000300.SH", "equity", False, 1, 0.005, 0.001, 4.2e9, "baostock"),
            ("510500.SH", "中证500ETF", "000905.SH", "equity", False, 1, 0.005, 0.001, 3.1e9, "baostock"),
            ("513100.SH", "纳指ETF", None, "qdie", True, 2, 0.008, 0.002, 1.2e9, "baostock"),
        ]
        con.executemany(
            "INSERT INTO etf_meta (symbol, name, track_index, fund_type, is_cross_border, "
            "sellable_after_days, management_fee, custody_fee, fund_size, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (symbol) DO UPDATE SET name = excluded.name",
            rows)


def test_etf_meta_enveloped(client):
    _seed_etf_meta()
    r = client.get("/api/etf/meta")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"code", "data", "message", "trace_id"}
    metas = {m["symbol"]: m for m in body["data"]}
    assert "510300.SH" in metas
    assert metas["510300.SH"]["track_index"] == "000300.SH"
    assert metas["513100.SH"]["is_cross_border"] is True
    # 搜索过滤
    hit = client.get("/api/etf/meta", params={"q": "纳指"}).json()["data"]
    assert len(hit) == 1 and hit[0]["symbol"] == "513100.SH"
    # 单只 404
    assert client.get("/api/etf/by-symbol/999999.SH").status_code == 404


def test_etf_correlation_empty_graceful(client):
    """湖无 ETF 行情 → 空 pairs + note，不 500（空态契约）。"""
    r = client.get("/api/etf/correlation")
    assert r.status_code == 200
    body = r.json()["data"]
    assert "pairs" in body and "note" in body


def test_etf_correlation_computes_pairs(client, monkeypatch):
    """有 ETF 日线时算得出 pairs 且 col 序/三角解开正确（回归：polars 无 DF.pct_change）。"""

    # correlation 内 `from lquant.data.store.parquet import read_daily` → 打在源模块上。
    monkeypatch.setattr("lquant.data.store.parquet.read_daily", lambda: df_lake())

    r = client.get("/api/etf/correlation", params={"horizon": 30})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["note"] is None
    syms = body["symbols"]
    assert syms == ["510001.SH", "510002.SH", "510003.SH"]
    # A 与 C 同构单调（1..6 / 10..60）→ 收益率近乎完美相关；pairs=3 且角标映射正确。
    by = {(p["a"], p["b"]): p["corr"] for p in body["pairs"]}
    assert by[(syms[0], syms[2])] >= 0.99
    assert len(body["pairs"]) == 3


def df_lake():
    """合成 3 只沪 ETF 的 20 日日线湖（15+ 根有效收益率，触发真实计算路径）。"""
    import polars as pl

    b_close = [1, 40, 2, 43, 4, 41, 7, 44, 9, 47, 11, 50, 13, 53, 15, 56, 17, 59, 19, 62]
    rows = []
    for d, ci in enumerate(range(1, 21), start=1):
        c = ci * 1.0
        rows.append((d, "510001.SH", c, c))
        rows.append((d, "510002.SH", float(b_close[ci - 1]), float(b_close[ci - 1])))
        rows.append((d, "510003.SH", ci * 10.0, ci * 10.0))
    # read_daily 契约返回 LazyFrame（.collect() 物化）—— stub 须保持一致。
    return pl.DataFrame(rows, schema=["trade_date", "symbol", "close", "pre_close"]).lazy()


# ---------- market 补充 ----------

def test_market_snapshot(client):
    client.post("/api/market/collect", json={"demo": True})
    r = client.get("/api/market/snapshot", params={"page": 1, "size": 10})
    assert r.status_code == 200
    body = r.json()
    assert {"trade_date", "total", "rows"} <= set(body)
    if body["rows"]:
        assert body["rows"][0]["change_pct"] >= body["rows"][-1]["change_pct"]
    # 非法 sort → 422；合法 turnover_rate 通过
    assert client.get("/api/market/snapshot",
                      params={"sort": "bogus"}).status_code == 422
    assert client.get("/api/market/snapshot",
                      params={"sort": "turnover_rate"}).status_code == 200


def test_market_heat_and_schedules(client):
    h = client.get("/api/market/heat")
    assert h.status_code == 200
    hb = h.json()
    assert {"gainers", "losers", "volume", "dragon_tiger"} <= set(hb)

    s = client.get("/api/market/schedules")
    assert s.status_code == 200
    sb = s.json()
    assert {"schedules", "coverage"} <= set(sb)
    names = {x["name"] for x in sb["schedules"]}
    assert {"close", "evening"} <= names


# ---------- WebSocket ----------

def test_ws_job_not_found(client):
    with client.websocket_connect("/ws/jobs/ghost-job") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "not_found" and msg["done"] is True


def test_ws_market_ticks_degrades_offline(client, monkeypatch):
    """实时链路断时推送 available=false 帧后关连（可降级契约）。"""
    from lquant.market import ticks
    from lquant.market.ticks import TicksError

    def _raise(symbols, **kw):
        raise TicksError("no source")

    monkeypatch.setattr(ticks, "fetch_quotes", _raise)
    with client.websocket_connect("/ws/market/ticks?symbols=600519") as ws:
        msg = ws.receive_json()
    assert msg["available"] is False and msg["error"]


def test_ws_market_ticks_empty_symbols(client):
    """无标的直接降级关连，不挂空连接。"""
    with client.websocket_connect("/ws/market/ticks") as ws:
        msg = ws.receive_json()
    assert msg["available"] is False and "symbols" in msg["error"]


# ---------- 参考数据（退市名单）同步 ----------

def test_reference_sync_endpoint(client, monkeypatch):
    """POST /data/reference/sync → 202，后台任务执行一次；重入 → 409。"""
    import threading

    from lquant.server.api import data as data_mod

    calls: list[dict] = []
    done = threading.Event()

    fake_result = {"calendar": 1, "securities": 1, "delisted": 2, "details": 0}

    def _fake_sync(skip_details=False, detail_limit=None):
        calls.append({"skip_details": skip_details})
        done.set()
        return fake_result

    monkeypatch.setattr(data_mod, "sync_reference", _fake_sync)
    monkeypatch.setattr(data_mod, "_ref_lock", threading.Lock())

    r = client.post("/api/data/reference/sync", json={"sync_details": False})
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert done.wait(5), "后台任务未执行"
    assert calls == [{"skip_details": True}]

    # 上一次同步完成释放锁后可再次触发
    done.clear()
    r2 = client.post("/api/data/reference/sync", json={})
    assert r2.status_code == 202
    assert done.wait(5)

    # 锁被占（上一次同步未结束）→ 409
    monkeypatch.setattr(data_mod, "_ref_lock", threading.Lock())
    assert data_mod._ref_lock.acquire(blocking=False)
    r3 = client.post("/api/data/reference/sync", json={})
    assert r3.status_code == 409
