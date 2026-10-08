"""实验记录器 backtest/runs.py（借鉴 qlib R 记录器）：落库/清单/详情/对比。

隔离姿势沿用 test_demo_overwrite_guard：LQ_ROOT + chdir + cache_clear，
DDL 只建 backtest_run 相关表（runs.py 只碰这一张）。
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime

import pytest
from click.testing import CliRunner


@pytest.fixture
def runs_env(tmp_path, monkeypatch):
    """隔离 DuckDB：LQ_ROOT 指向 tmp_path，建齐 DDL。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    yield tmp_path
    get_settings.cache_clear()


# ---------------- git_hash ----------------


def test_git_hash_none_when_subprocess_fails(monkeypatch):
    from lquant.backtest import runs

    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(runs.subprocess, "run", boom)
    assert runs.git_hash() is None


def test_git_hash_returns_short_hash_in_repo():
    from lquant.backtest import runs

    h = runs.git_hash()
    # cwd 固定到仓库根：测试无论从哪个 cwd 跑，repo 内都能拿到 hash
    assert h is not None
    assert 7 <= len(h) <= 40


def test_git_hash_ignores_foreign_git_repo(tmp_path, monkeypatch):
    """cwd 落在别的 git 仓库时不误记异库 hash：cwd 固定到 lquant 仓库根。"""
    import subprocess as sp

    from lquant.backtest import runs

    # 造一个独立的 git 仓库当「异库」
    monkeypatch.chdir(tmp_path)
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sp.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "x"],
        cwd=tmp_path,
        check=True,
    )

    h = runs.git_hash()
    assert h is not None
    # 与 lquant 仓库 HEAD 一致（不是 tmp 异库的 hash）
    head = sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=runs._REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert h == head


def test_git_hash_timeout_is_swallowed(monkeypatch):
    """git 卡死也不阻断记录：timeout 参数在，异常被吞。"""
    from lquant.backtest import runs

    captured = {}

    def slow(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(runs.subprocess, "run", slow)
    assert runs.git_hash() is None
    assert captured["timeout"] == 5


# ---------------- record / list / get ----------------


def test_record_run_self_heals_missing_table(tmp_path, monkeypatch):
    """老库缺 ``backtest_run`` 表时记录不该失败（默认落库路径必须自愈）。

    这张表只在 ``DDL_STATEMENTS`` 里（init_db / 服务启动才执行），而
    ``lq backtest run`` 默认记录 —— 缺表会让「记一条实验」变成一条警告，
    连 stdout 的 JSON 都被污染（CLI 的 --json 输出要求纯 JSON）。
    """
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    con.execute("CREATE TABLE unrelated (x INTEGER)")  # 库存在但没建 DDL
    con.close()

    from lquant.backtest import runs

    rid = runs.record_run("factor_quantile", {"factor": "mom_20"}, {"monotonicity": 1})
    assert rid and len(rid) == 12
    assert [r["run_id"] for r in runs.list_runs()] == [rid]
    get_settings.cache_clear()


def test_record_and_list_roundtrip(runs_env):
    from lquant.backtest import runs

    rid = runs.record_run(
        "factor_quantile",
        {"factor": "mom_20", "n_groups": 5},
        {"monotonicity": 1, "long_short": {"annual": 0.12}},
        start_date="2024-01-02",
        end_date="2024-06-28",
    )
    assert rid and len(rid) == 12

    got = runs.list_runs(limit=10)
    assert len(got) == 1
    assert got[0]["run_id"] == rid
    assert got[0]["strategy"] == "factor_quantile"
    assert got[0]["params"]["factor"] == "mom_20"
    assert got[0]["metrics"]["long_short"]["annual"] == pytest.approx(0.12)
    assert got[0]["status"] == "done"


def test_record_attaches_git_hash(monkeypatch, runs_env):
    from lquant.backtest import runs

    monkeypatch.setattr(runs, "git_hash", lambda: "abc1234")
    runs.record_run("s", {"k": 1}, {})
    r = runs.list_runs()[0]
    assert r["params"]["git_hash"] == "abc1234"


def test_record_without_git_no_hash_key(monkeypatch, runs_env):
    from lquant.backtest import runs

    monkeypatch.setattr(runs, "git_hash", lambda: None)
    runs.record_run("s", {"k": 1}, {})
    assert "git_hash" not in runs.list_runs()[0]["params"]


def test_record_nan_metric_becomes_null(runs_env):
    """NaN/Inf 落库前归 None（与 API 层 _json_safe 同规，保证合法 JSON）。"""
    import duckdb

    from lquant.backtest import runs
    from lquant.core.config import get_settings

    runs.record_run("s", {}, {"sharpe": float("nan"), "ic": float("inf"), "ok": 1.5})
    m = runs.list_runs()[0]["metrics"]
    assert m["sharpe"] is None and m["ic"] is None
    assert m["ok"] == pytest.approx(1.5)
    # 落库原文必须是合法 JSON（字面量 NaN 会让 json.loads 抛错）
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    raw = con.execute("SELECT metrics FROM backtest_run").fetchone()[0]
    con.close()
    assert json.loads(raw) == m


def test_record_bad_date_fails_loudly(runs_env):
    """非法日期在记账前就抛（不写半条记录）。"""
    from lquant.backtest import runs

    with pytest.raises(ValueError):
        runs.record_run("s", {}, {}, start_date="2024/13/01")


def test_api_persist_result_writes_cn_wall_clock(runs_env, monkeypatch):
    """Web 路径的 created_at/finished_at 必须走 CN 墙钟（与 CLI 同源）。

    ``backtest_run`` 由 CLI（``runs.record_run`` → ``now_cn_naive``）与 Web
    （``server/api/backtests._persist_result``）两个写入方共用，``list_runs``
    的 ``ORDER BY created_at DESC`` 只有在两者同一时区时才正确。本机默认
    TZ 恰好是 Asia/Shanghai，裸 ``datetime.now()`` 在这里看不出差别 ——
    所以直接给 ``now_cn_naive`` 打哨兵：Web 路径若回退成裸本地时间，写进去
    的就不再是哨兵值，这条立刻红。
    """
    import duckdb
    import polars as pl

    from lquant.core import types as core_types
    from lquant.core.config import get_settings
    from lquant.server.api.backtests import _persist_result

    class _FakeRes:
        def __init__(self, nav):
            self._nav = nav
            self.metrics = {"n_trades": 0}
            self.positions = {}

        def to_frame(self):
            return self._nav

        def trades_frame(self):
            return pl.DataFrame({
                "trade_date": [], "symbol": [], "side": [],
                "qty": [], "price": [], "fee": [],
            })

    sentinel = datetime(2019, 3, 4, 5, 6, 7)
    monkeypatch.setattr(core_types, "now_cn_naive", lambda: sentinel)

    _persist_result(
        "rid_cn_clock", "factor_topn", {},
        _FakeRes(pl.DataFrame({"trade_date": [date(2024, 1, 2)], "nav": [1.0]})),
    )

    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    created, finished = con.execute(
        "SELECT created_at, finished_at FROM backtest_run WHERE run_id = 'rid_cn_clock'"
    ).fetchone()
    con.close()
    assert created == sentinel
    assert finished == sentinel


def test_record_run_uses_cn_wall_clock(runs_env, monkeypatch):
    """CLI 写入方（runs.record_run）与 Web 共用同一个 CN 墙钟函数。"""
    import duckdb

    from lquant.backtest import runs
    from lquant.core import types as core_types
    from lquant.core.config import get_settings

    sentinel = datetime(2019, 3, 4, 5, 6, 7)
    monkeypatch.setattr(core_types, "now_cn_naive", lambda: sentinel)

    rid = runs.record_run("s", {}, {})
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    created = con.execute(
        "SELECT created_at FROM backtest_run WHERE run_id = ?", [rid]).fetchone()[0]
    con.close()
    assert created == sentinel


def test_get_run_missing_raises(runs_env):
    from lquant.backtest import runs

    with pytest.raises(KeyError):
        runs.get_run("nope")


def test_record_duplicate_run_id_raises(runs_env, monkeypatch):
    """撞键必须报错而非静默覆盖旧实验（OR REPLACE 的数据丢失风险已除）。"""
    import uuid as uuid_mod

    from lquant.backtest import runs

    monkeypatch.setattr(runs.uuid, "uuid4", lambda: uuid_mod.UUID("ab" * 16))
    runs.record_run("s", {"k": 1}, {})
    with pytest.raises(Exception):  # noqa: B017 - duckdb.ConstraintException
        runs.record_run("s", {"k": 2}, {})
    # 旧记录原样保留
    assert runs.list_runs()[0]["params"]["k"] == 1


def test_list_runs_strategy_filter(runs_env):
    from lquant.backtest import runs

    runs.record_run("factor_quantile", {}, {})
    runs.record_run("jq_code", {}, {})
    assert [r["strategy"] for r in runs.list_runs(strategy="jq_code")] == ["jq_code"]
    assert len(runs.list_runs()) == 2


# ---------------- diff ----------------


def test_diff_runs_key_level(runs_env):
    from lquant.backtest import runs

    ra = runs.record_run("s", {"factor": "mom_20", "n_groups": 5}, {"annual": 0.10, "sharpe": 1.0})
    rb = runs.record_run(
        "s", {"factor": "mom_60", "n_groups": 10}, {"annual": 0.15, "drawdown": 0.08}
    )
    d = runs.diff_runs(ra, rb)

    assert d["params"]["only_a"] == {}
    assert d["params"]["only_b"] == {}
    assert d["params"]["changed"] == {"factor": ["mom_20", "mom_60"], "n_groups": [5, 10]}
    assert d["metrics"]["changed"] == {"annual": [pytest.approx(0.10), pytest.approx(0.15)]}
    assert set(d["metrics"]["only_a"]) == {"sharpe"}
    assert set(d["metrics"]["only_b"]) == {"drawdown"}


def test_diff_runs_missing_id_raises(runs_env):
    from lquant.backtest import runs

    rid = runs.record_run("s", {}, {})
    with pytest.raises(KeyError):
        runs.diff_runs(rid, "ghost")


# ---------------- CLI ----------------


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, list(args))


def test_cli_list_empty_and_nonempty(runs_env):
    from lquant.backtest import runs

    r = _invoke("backtest", "list")
    assert r.exit_code == 0
    assert "暂无回测记录" in r.output

    runs.record_run("factor_quantile", {"factor": "mom_20"}, {"annual": 0.1})
    r2 = _invoke("backtest", "list")
    assert r2.exit_code == 0
    assert "factor_quantile" in r2.output
    assert "mom_20" in r2.output  # params 摘要可见


def test_cli_show_and_missing(runs_env):
    from lquant.backtest import runs

    rid = runs.record_run("s", {"k": 1}, {"m": 2})
    r = _invoke("backtest", "show", rid)
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["run_id"] == rid and out["metrics"]["m"] == 2

    r2 = _invoke("backtest", "show", "ghost")
    assert r2.exit_code != 0
    assert "实验不存在" in r2.output


def test_cli_diff_output(runs_env):
    from lquant.backtest import runs

    ra = runs.record_run("s", {"n": 5}, {"annual": 0.1})
    rb = runs.record_run("s", {"n": 10}, {"annual": 0.2})
    r = _invoke("backtest", "diff", ra, rb)
    assert r.exit_code == 0
    assert "变更 n: 5  ->  10" in r.output
    assert "变更 annual:" in r.output

    r2 = _invoke("backtest", "diff", ra, "ghost")
    assert r2.exit_code != 0
    assert "实验不存在" in r2.output


def test_cli_run_rejects_missing_input(runs_env):
    """run 无 --factor/--spec 时快速失败（不落库、不碰湖）。"""
    r = _invoke("backtest", "run")
    assert r.exit_code != 0
    assert "需要 --factor" in r.output
