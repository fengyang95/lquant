"""C4：portfolio 包接线到 CLI（``lq portfolio``）与 API（``/api/portfolio/*``）。

隔离姿势沿用 ``test_board_features.board_env``：LQ_ROOT 指向 tmp + chdir +
``get_settings.cache_clear()`` + 全量 DDL。这里额外做两件本任务专属的事：

1. **真读湖**：日线用 ``data.store.parquet.write_daily`` 写进 tmp 的 Parquet
   湖，再由 ``lq portfolio screen`` 经 ``read_daily`` 读回 —— 端到端验证
   「全市场横截面选池」真的走本地湖，而不是靠 mock。
2. **可控的过滤场景**：8 只标的里塞进 ST / 次新 / 停牌 / 一字板 / 低流动性
   各一只，断言它们被各自的过滤器剔掉、且剩下的 3 只按动量排序。

为什么用 module 作用域的 env：API 侧要用 ``TestClient``，每次建 app 都跑一遍
startup（监控、同步播种）代价高；CLI 侧只是同进程调 click，共用同一份隔离
湖即可，也顺带保证两个入口看到的是**同一份数据**。
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import polars as pl
import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")

# ---- 场景常量：5 个交易日禁用不上，用 30 个工作日把 mom20 撑起来 ----
def _business_days(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _business_days(30)
TARGET = DAYS[-1]
TARGET_ISO = TARGET.isoformat()

# (symbol, name, is_st, 日漂移, 特殊角色)
SPECS = [
    ("600000.SH", "浦发银行", False, 0.012, "normal-top"),
    ("600036.SH", "招商银行", False, 0.006, "normal-mid"),
    ("601318.SH", "中国平安", False, -0.004, "normal-bot"),
    ("000001.SZ", "ST平安", True, 0.020, "st"),
    ("300750.SZ", "宁德时代", False, 0.030, "new"),
    ("000002.SZ", "万科A", False, 0.005, "suspended"),
    ("000333.SZ", "美的集团", False, 0.004, "limit-up"),
    ("000651.SZ", "格力电器", False, 0.001, "illiquid"),
]
SYMS = [s for s, *_ in SPECS]
EXPECTED_KEPT = ["600000.SH", "600036.SH", "601318.SH"]  # 其余 5 只各被一条过滤剔掉
NEW_LIST_DATE = TARGET - timedelta(days=10)  # < 60 天 → 次新


def _daily_df() -> pl.DataFrame:
    rows = []
    for j, (sym, _name, _st, drift, role) in enumerate(SPECS):
        base = 10.0 + j * 5.0
        for i, d in enumerate(DAYS):
            close = base * (1 + drift) ** i
            pre_close = base * (1 + drift) ** (i - 1) if i else base
            high, low = close * 1.01, close * 0.99
            volume = 1e6
            amount = volume * close
            if role == "limit-up" and d == TARGET:
                # 一字板：high == low == close 且正好 +10%（不 round，避免
                # round 把 close 压到 pre_close*1.1 - 1e-9 之下而躲过判定）
                pre_close = base * (1 + drift) ** (i - 1)
                close = pre_close * 1.10
                high = low = close
                amount = volume * close
            if role == "suspended" and d == TARGET:
                volume = 0.0
                amount = 0.0
            if role == "illiquid":
                amount = 1e5  # < 5e6 下限
            rows.append({
                "trade_date": d, "symbol": sym,
                "open": close, "high": high, "low": low, "close": close,
                "pre_close": pre_close, "volume": volume, "amount": amount,
                "turnover_rate": 1.0 + j, "adj_factor": 1.0,
                "float_mv": base * 1e8,
            })
    return pl.DataFrame(rows).with_columns(
        pl.col("trade_date").cast(pl.Date), pl.col("pre_close").cast(pl.Float64)
    )


@pytest.fixture(scope="module")
def portfolio_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("portfolio_c4")
    prev_cwd = os.getcwd()
    prev_root = os.environ.get("LQ_ROOT")
    os.chdir(base)
    os.environ["LQ_ROOT"] = str(base)

    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS

    (base / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    for sym, name, is_st, _drift, _role in SPECS:
        con.execute(
            "INSERT INTO security (symbol, name, sec_type, list_date, is_st) "
            "VALUES (?, ?, 'stock', ?, ?)",
            [sym, name, NEW_LIST_DATE if sym == "300750.SZ" else date(2010, 1, 1), is_st],
        )
    con.close()

    from lquant.data.store.parquet import write_daily

    write_daily(_daily_df())
    yield base

    os.chdir(prev_cwd)
    if prev_root is None:
        os.environ.pop("LQ_ROOT", None)
    else:
        os.environ["LQ_ROOT"] = prev_root
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(portfolio_env):
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------------------------------------------------------- CLI


def _invoke(args: list[str]):
    from click.testing import CliRunner

    from lquant.cli.main import cli

    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_portfolio_help_lists_subcommands(portfolio_env):
    """验收：`lq portfolio --help` 必须列出全部子命令（接线生效）。"""
    r = _invoke(["portfolio", "--help"])
    assert r.exit_code == 0, r.output
    for sub in ("screen", "size", "weights", "optimize", "methods"):
        assert sub in r.stdout, f"子命令 {sub} 未注册\n{r.stdout}"


def test_screen_reads_real_lake_end_to_end(portfolio_env):
    """真实读湖的端到端：先过滤后打分，Top-N 与过滤层的预期一致。"""
    r = _invoke(["portfolio", "screen", "--date", TARGET_ISO, "--top-n", "3"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)  # stdout 必须是**纯净**可解析 JSON
    assert payload["trade_date"] == TARGET_ISO
    assert payload["filter_report"]["total"] == len(SYMS)
    assert payload["filter_report"]["kept"] == len(EXPECTED_KEPT)
    assert payload["meta_available"] is True
    assert payload["warnings"] == []
    assert [p["symbol"] for p in payload["picks"]] == EXPECTED_KEPT
    # 打分口径与因子一起回传，Agent 才能解释排名
    assert payload["factors"] == {"mom20": 1.0}
    top = payload["picks"][0]
    assert top["symbol"] == "600000.SH"
    assert top["score"] > payload["picks"][1]["score"] > payload["picks"][2]["score"]


def test_screen_default_date_uses_latest(portfolio_env):
    r = _invoke(["portfolio", "screen", "--top-n", "1"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["trade_date"] == TARGET_ISO


def test_screen_filter_flags_toggle(portfolio_env):
    """关掉 ST/次新/一字板过滤 → 更多的票进来（证明过滤参数真的接上了）。"""
    r = _invoke([
        "portfolio", "screen", "--date", TARGET_ISO, "--top-n", "10",
        "--keep-st", "--min-list-days", "0", "--keep-limit-up",
    ])
    assert r.exit_code == 0, r.output
    kept = json.loads(r.stdout)["filter_report"]["kept"]
    assert kept > len(EXPECTED_KEPT)


def test_screen_bad_date_fails_loudly(portfolio_env):
    r = _invoke(["portfolio", "screen", "--date", "1990-01-01"])
    assert r.exit_code == 1
    assert "无日线" in r.output and "lq data" in r.output


def test_screen_bad_factor_fails_loudly(portfolio_env):
    r = _invoke(["portfolio", "screen", "--date", TARGET_ISO, "--factor", "nope"])
    assert r.exit_code == 1
    assert "因子列不存在" in r.output


def test_screen_bad_top_n_fails_loudly(portfolio_env):
    r = _invoke(["portfolio", "screen", "--date", TARGET_ISO, "--top-n", "0"])
    assert r.exit_code == 1
    assert "top-n" in r.output


def test_size_atr_and_kelly(portfolio_env):
    r = _invoke(["portfolio", "size", "--close", "10", "--atr", "0.5",
                 "--daily-risk", "0.01", "--max-weight", "0.3"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["weight"] == pytest.approx(0.2)

    r2 = _invoke(["portfolio", "size", "--model", "kelly", "--win-rate", "0.6",
                  "--win-loss-ratio", "1.5", "--kelly-fraction", "0.5"])
    assert r2.exit_code == 0, r2.output
    # half-Kelly: 0.5 * (0.6 - 0.4/1.5) = 0.1666...
    assert json.loads(r2.stdout)["weight"] == pytest.approx(1 / 6)


def test_size_failure_paths(portfolio_env):
    r = _invoke(["portfolio", "size", "--close", "10"])  # 缺 --atr
    assert r.exit_code == 1
    assert "--atr" in r.output
    r2 = _invoke(["portfolio", "size", "--model", "kelly", "--win-rate", "1.5",
                  "--win-loss-ratio", "1"])
    assert r2.exit_code == 1
    assert "win_rate" in r2.output


def test_weights_registered_method(portfolio_env):
    r = _invoke(["portfolio", "weights", "--method", "inverse_vol",
                 "--symbols", ",".join(EXPECTED_KEPT), "--end", TARGET_ISO])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert set(payload["weights"]) == set(EXPECTED_KEPT)
    assert sum(payload["weights"].values()) == pytest.approx(1.0)
    assert payload["report"][0]["method"] == "inverse_vol"
    assert payload["report"][0]["n_holdings"] == 3


def test_weights_score_and_market_cap_unregistered_methods(portfolio_env):
    r = _invoke(["portfolio", "weights", "--method", "score_weight",
                 "--score", "600000.SH=2,600036.SH=1,601318.SH=1"])
    assert r.exit_code == 0, r.output
    w = json.loads(r.stdout)["weights"]
    assert w["600000.SH"] == pytest.approx(0.5)

    r2 = _invoke(["portfolio", "weights", "--method", "market_cap_weight",
                  "--symbols", ",".join(EXPECTED_KEPT), "--end", TARGET_ISO])
    assert r2.exit_code == 0, r2.output
    assert sum(json.loads(r2.stdout)["weights"].values()) == pytest.approx(1.0)


def test_weights_failure_paths(portfolio_env):
    r = _invoke(["portfolio", "weights", "--method", "bogus",
                 "--symbols", "600000.SH"])
    assert r.exit_code == 1
    assert "未知方法" in r.output
    r2 = _invoke(["portfolio", "weights", "--method", "hrp",
                  "--symbols", ",".join(EXPECTED_KEPT), "--band", "0.02"])
    assert r2.exit_code == 1
    assert "--prev" in r2.output


def test_methods_lists_registry(portfolio_env):
    r = _invoke(["portfolio", "methods"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    names = {m["name"] for m in payload["registered"]}
    assert {"equal", "inverse_vol", "hrp", "min_variance"} <= names
    assert {"score_weight", "market_cap_weight"} == {
        m["name"] for m in payload["unregistered"]
    }


def test_optimize_end_to_end(portfolio_env):
    scores = "600000.SH=1,600036.SH=0.5,601318.SH=-1"
    r = _invoke(["portfolio", "optimize", "--symbols", ",".join(EXPECTED_KEPT),
                 "--score", scores, "--te", "0.05", "--max-weight", "0.6"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert sum(payload["weights"].values()) == pytest.approx(1.0)
    assert set(payload["weights"]) == set(EXPECTED_KEPT)
    assert payload["diagnostics"]["fallback"] is False
    assert payload["diagnostics"]["constraint_violations"] == []

    # max_turnover 缺 prev_weights → 底层 OptimizerError 被转成可操作提示
    r2 = _invoke(["portfolio", "optimize", "--symbols", ",".join(EXPECTED_KEPT),
                  "--score", scores, "--max-turnover", "0.1"])
    assert r2.exit_code == 1
    assert "prev_weights" in r2.output or "上期权重" in r2.output


# ---------------------------------------------------------------- API


def _openapi_paths() -> dict:
    from lquant.server.main import create_app

    return create_app().app.openapi()["paths"]


def test_openapi_exposes_portfolio_paths(portfolio_env):
    paths = _openapi_paths()
    for p in ("/api/portfolio/methods", "/api/portfolio/screen",
              "/api/portfolio/size", "/api/portfolio/weights",
              "/api/portfolio/optimize"):
        assert p in paths, f"OpenAPI 缺 {p}"


def test_api_methods(client):
    r = client.get("/api/portfolio/methods")
    assert r.status_code == 200
    assert any(m["name"] == "equal" for m in r.json()["registered"])


def test_api_screen(client):
    r = client.post("/api/portfolio/screen",
                    json={"date": TARGET_ISO, "top_n": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filter_report"]["kept"] == len(EXPECTED_KEPT)
    assert [p["symbol"] for p in body["picks"]] == EXPECTED_KEPT


def test_api_screen_bad_date_is_404(client):
    r = client.post("/api/portfolio/screen", json={"date": "1990-01-01"})
    assert r.status_code == 404
    assert "无日线" in r.json()["detail"]


def test_api_screen_validation_is_422(client):
    assert client.post("/api/portfolio/screen", json={"top_n": 0}).status_code == 422


def test_api_size(client):
    r = client.post("/api/portfolio/size",
                    json={"model": "atr", "close": 10, "atr": 0.5, "max_weight": 0.3})
    assert r.status_code == 200
    assert r.json()["weight"] == pytest.approx(0.2)

    r2 = client.post("/api/portfolio/size", json={"model": "atr", "close": 10})
    assert r2.status_code == 422
    assert "--atr" in r2.json()["detail"]


def test_api_weights(client):
    r = client.post("/api/portfolio/weights", json={
        "method": "inverse_vol", "symbols": EXPECTED_KEPT, "end": TARGET_ISO,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(body["weights"].values()) == pytest.approx(1.0)
    assert body["report"][0]["method"] == "inverse_vol"

    r2 = client.post("/api/portfolio/weights",
                     json={"method": "bogus", "symbols": ["600000.SH"]})
    assert r2.status_code == 422
    assert "未知方法" in r2.json()["detail"]


def test_api_optimize(client):
    r = client.post("/api/portfolio/optimize", json={
        "symbols": EXPECTED_KEPT,
        "scores": {"600000.SH": 1.0, "600036.SH": 0.5, "601318.SH": -1.0},
        "te_target": 0.05, "max_weight": 0.6,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(body["weights"].values()) == pytest.approx(1.0)
    assert body["diagnostics"]["constraint_violations"] == []
