"""`lq data` CLI 子命令覆盖补齐：下层采集函数一律打桩，只测 CLI 参数流与输出。"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import polars as pl
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["data", *args])


def _patch_mod(monkeypatch, dotted, name, fn):
    import importlib

    mod = importlib.import_module(dotted)
    monkeypatch.setattr(mod, name, fn)


def test_reference_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.reference", "sync_reference",
               lambda **kw: {"calendar": 1, "security": 2} | {"skip_details": kw["skip_details"]})
    r = _invoke("reference", "--skip-details", "--detail-limit", "5")
    assert r.exit_code == 0, r.output
    assert "reference done" in r.output


def test_sync_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.daily", "backfill_daily",
               lambda **kw: 42)
    r = _invoke("sync", "--start", "2020-01-01", "--end", "2020-12-31")
    assert r.exit_code == 0, r.output
    assert "done 42" in r.output


def test_etf_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.etf_meta", "sync_etf_meta",
               lambda syms=None: len(syms or []))
    _patch_mod(monkeypatch, "lquant.data.ingest.etf_meta", "enrich_from_akshare",
               lambda: 7)
    r = _invoke("etf", "--symbols", "510300.SH,159915.SZ", "--enrich")
    assert r.exit_code == 0, r.output
    assert "etf_meta done 2" in r.output
    assert "enrich done 7" in r.output


def test_minute_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.minute", "backfill_minute",
               lambda syms, **kw: len(syms))
    r = _invoke("minute", "--symbols", "510300.SH,159915.SZ", "--freq", "15min")
    assert r.exit_code == 0, r.output
    assert "done 2" in r.output


def _financial_stub(syms, **kw):
    """backfill_financial 现在返回窗口记账 dict（不再只回一个数）。"""
    return {"done": len(syms), "skipped_covered": 0, "groups": 1,
            "start": "2016-01-01", "end": "2026-09-18", "rows": len(syms)}


def test_financial_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.financial", "backfill_financial",
               _financial_stub)
    r = _invoke("financial", "--symbols", "600000.SH,000001.SZ", "--provider", "baostock")
    assert r.exit_code == 0, r.output
    assert "实拉 2 只" in r.output and "覆盖区间跳过 0 只" in r.output


def test_financial_all_and_missing_args(monkeypatch):
    import lquant.data.store.catalog as catalog_mod

    _patch_mod(monkeypatch, "lquant.data.ingest.financial", "backfill_financial",
               _financial_stub)
    monkeypatch.setattr(catalog_mod.SecurityRepo, "stock_symbols",
                        lambda self: ["600000.SH", "000001.SZ", "600519.SH"])
    r = _invoke("financial", "--all")
    assert r.exit_code == 0, r.output
    assert "实拉 3 只" in r.output

    r2 = _invoke("financial")
    assert r2.exit_code != 0
    assert "--symbols 与 --all 必须给一个" in r2.output


def test_basic_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.daily_basic", "backfill_daily_basic",
               lambda **kw: {"rows": 10, "merged": 3})
    r = _invoke("basic", "--no-merge")
    assert r.exit_code == 0, r.output
    assert "basic done" in r.output


def test_index_cons_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.index_cons", "sync_index_cons",
               lambda codes=None: {"codes": len(codes or [])})
    r = _invoke("index-cons", "--indexes", "000300.SH,000905.SH")
    assert r.exit_code == 0, r.output
    assert "index-cons done" in r.output


class _RaisingReader:
    def __enter__(self):
        raise RuntimeError("db down")

    def __exit__(self, *a):
        return False


class _OkReader:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        con = SimpleNamespace(execute=lambda sql: SimpleNamespace(
            fetchone=lambda: (7, "2024-01-02", "2026-06-30")))
        return con

    def __exit__(self, *a):
        return False


def test_status_cmd_error_branch(monkeypatch):
    import lquant.core.db as db_mod

    monkeypatch.setattr(db_mod, "reader", lambda: _RaisingReader())
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    assert "RuntimeError" in r.output
    assert "daily parquet 年分区:" in r.output


def test_status_cmd_ok(monkeypatch):
    """reader 正常返回 → 打印各表行数（成功分支）。"""
    import lquant.core.db as db_mod

    class _Ok:
        def __enter__(self):
            return SimpleNamespace(execute=lambda sql: SimpleNamespace(
                fetchone=lambda: (7,)))

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db_mod, "reader", lambda: _Ok())
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    assert "security: 7" in r.output
    assert "financial_pit: 7" in r.output


def test_demo_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.demo", "generate_demo",
               lambda **kw: {"symbols": 3, "days": 5})
    r = _invoke("demo", "--start", "2025-01-01")
    assert r.exit_code == 0, r.output
    assert "demo data done" in r.output


def _issue(sev, rule):
    return SimpleNamespace(severity=sev, rule=rule, detail="d")


def test_check_pass(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [])
    r = _invoke("check")
    assert r.exit_code == 0, r.output
    assert "quality: PASS" in r.output


def test_check_warn_only(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [_issue("warn", "R1"), _issue("warn", "R2")])
    r = _invoke("check")
    assert r.exit_code == 0, r.output
    assert "2 条 issue" in r.output


def test_check_fatal_exits_2(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [_issue("fatal", "DUP_KEY"), _issue("error", "R9")])
    r = _invoke("check")
    assert r.exit_code == 2
    assert "DUP_KEY" in r.output


def test_crosscheck_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.crosscheck", "run_crosscheck",
               lambda **kw: {"summary": {"L1": 1, "L2": 0, "L3": 0},
                             "issues": [_issue("error", "X_L1")],
                             "flagged_rows": 12})
    r = _invoke("crosscheck", "--peers", "a,b", "--limit", "50")
    assert r.exit_code == 0, r.output
    assert "crosscheck:" in r.output
    assert "[L1] 1 条 issue" in r.output
    assert "12 行" in r.output


def test_fields_cmd(monkeypatch):
    df = pl.DataFrame({"close": [1.0, None, 3.0], "volume": [1, 2, 3]})
    _patch_mod(monkeypatch, "lquant.data.store.parquet", "read_daily",
               lambda start=None: df.lazy())
    r = _invoke("fields")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    cov = {d["field"]: d["coverage"] for d in body}
    assert cov["close"] == 0.6667 and cov["volume"] == 1.0


def test_fields_empty_raises(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.store.parquet", "read_daily",
               lambda start=None: pl.DataFrame().lazy())
    r = _invoke("fields")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def _pinned_root(tmp_path, lake):
    """写一个自带 config/app.yaml 的临时 LQ_ROOT。

    把 LQ_ROOT 一并钉住（而不是只靠 chdir）：`find_root()` 优先读 LQ_ROOT，
    没配才沿 `__file__` 上溯找 pyproject.toml。钉住后本用例的期望值与
    调用方 CWD、以及外层环境里的 LQ_ROOT（常被其他用例改过）都无关。
     """
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "app.yaml").write_text(
        "paths:\n"
        f'  parquet: "{lake}/parquet"\n'
        f'  duckdb: "{tmp_path}/lq.duckdb"\n'
        f'  cache: "{tmp_path}/cache"\n',
        encoding="utf-8",
    )
    return root


def test_status_counts_lake_from_settings_not_cwd(tmp_path, monkeypatch):
    """湖分区数按 settings.parquet_dir 统计，不能用相对 CWD 的 data/parquet。

    回归：旧实现写死 glob("data/parquet/daily/**/*.parquet")。湖在绝对
    路径（LQ_DATA_DIR）或服务与 CLI 的 CWD 不同时，这里会静默显示
    0 个分区 —— 与 parquet.lake_glob 的「SQL 侧必须用绝对 glob」同一约定。
    """
    lake = tmp_path / "lake"                      # 湖在 CWD 之外
    (lake / "parquet" / "daily" / "year=2024").mkdir(parents=True)
    (lake / "parquet" / "daily" / "year=2024" / "part-0.parquet").write_bytes(b"x")
    monkeypatch.setenv("LQ_ROOT", str(_pinned_root(tmp_path, lake)))
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_settings().parquet_dir == str(lake / "parquet")
        r = _invoke("status")
        assert r.exit_code == 0, r.output
        assert "daily parquet 年分区: 1" in r.output
        assert "湖为空" not in r.output
    finally:
        get_settings.cache_clear()


def test_status_reports_empty_lake_hint(tmp_path, monkeypatch):
    """空湖要显式提示先同步，而不是只报 0 个分区。"""
    lake = tmp_path / "lake"
    (lake / "parquet").mkdir(parents=True)
    monkeypatch.setenv("LQ_ROOT", str(_pinned_root(tmp_path, lake)))
    monkeypatch.chdir(tmp_path)

    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        r = _invoke("status")
        assert r.exit_code == 0, r.output
        assert "daily parquet 年分区: 0" in r.output
        assert "湖为空" in r.output
    finally:
        get_settings.cache_clear()


# ---------------- lq data money-flow ----------------
#
# 下层一律打桩：只测 CLI 的参数流与输出（命令体里 import，
# 所以打的是 lquant.market.backfill 的模块属性）。

_FLOW_REPORT = {
    "table": "money_flow", "symbols": 2, "days": 30, "qps": 1.5,
    "fetched": 40, "persisted": 40, "failed": {},
    "covered_before": 0, "covered_after": 2,
}


def _stub_flow(monkeypatch, report=None, symbols=("600519.SH", "000001.SZ")):
    seen: dict = {}

    def fake_backfill(targets, **kw):
        seen["targets"] = list(targets)
        seen.update(kw)
        return dict(report or _FLOW_REPORT)

    _patch_mod(monkeypatch, "lquant.market.backfill", "backfill_money_flow_history",
               fake_backfill)
    _patch_mod(monkeypatch, "lquant.market.backfill", "money_flow_symbols",
               lambda **kw: list(symbols))
    return seen


def test_money_flow_cmd_without_targets(monkeypatch):
    seen = _stub_flow(monkeypatch)
    r = _invoke("money-flow")
    assert r.exit_code == 0, r.output
    assert "未指定标的" in r.output
    assert "targets" not in seen          # 没指定就别去联网


def test_money_flow_cmd_symbols_and_days(monkeypatch):
    seen = _stub_flow(monkeypatch)
    r = _invoke("money-flow", "--symbols", "600519,000001.SZ", "--days", "30",
                "--qps", "0.5")
    assert r.exit_code == 0, r.output
    assert seen["targets"] == ["600519", "000001.SZ"]
    assert seen["days"] == 30 and seen["qps"] == 0.5
    assert "拉取: 40 行，入库: 40 行" in r.output
    assert "0 → 2" in r.output and "money-flow done" in r.output


def test_money_flow_cmd_all_uses_lake_symbols(monkeypatch):
    seen = _stub_flow(monkeypatch, symbols=("600519.SH",))
    r = _invoke("money-flow", "--all", "--limit", "7")
    assert r.exit_code == 0, r.output
    assert seen["targets"] == ["600519.SH"]
    assert "标的池（日线湖）: 1 只" in r.output


def test_money_flow_cmd_all_empty_lake(monkeypatch):
    seen = _stub_flow(monkeypatch, symbols=())
    r = _invoke("money-flow", "--all")
    assert r.exit_code == 0, r.output
    assert "日线湖为空" in r.output
    assert "targets" not in seen


def test_money_flow_cmd_reports_failures(monkeypatch):
    rep = dict(_FLOW_REPORT, failed={"600519.SH": "RuntimeError: 单票接口 500"},
               covered_after=1, persisted=20, fetched=20)
    _stub_flow(monkeypatch, report=rep)
    r = _invoke("money-flow", "--symbols", "600519.SH")
    assert r.exit_code == 0, r.output
    assert "失败 1 只" in r.output
    assert "单票接口 500" in r.output


def test_money_flow_cmd_purge_demo_needs_yes(monkeypatch):
    calls: list = []
    _patch_mod(monkeypatch, "lquant.market.backfill", "purge_demo_flow",
               lambda **kw: (calls.append(kw),
                             {"dry_run": True, "demo_rows": 599,
                              "relabel_rows": 900, "deleted": 0})[1])
    _patch_mod(monkeypatch, "lquant.market.backfill", "money_flow_symbols",
               lambda **kw: [])
    r = _invoke("money-flow", "--purge-demo")
    assert r.exit_code == 0, r.output
    assert "待清理合成数据: 599 行" in r.output
    assert "确认后加 --yes" in r.output
    assert calls == [{"dry_run": True}]      # 没 --yes 就不能真删


def test_money_flow_cmd_purge_demo_with_yes(monkeypatch):
    calls: list = []

    def fake_purge(**kw):
        calls.append(kw)
        if kw.get("dry_run"):
            return {"dry_run": True, "demo_rows": 599, "relabel_rows": 900,
                    "deleted": 0}
        return {"dry_run": False, "demo_rows": 599, "relabel_rows": 900,
                "deleted": 599}

    _patch_mod(monkeypatch, "lquant.market.backfill", "purge_demo_flow", fake_purge)
    _patch_mod(monkeypatch, "lquant.market.backfill", "money_flow_symbols",
               lambda **kw: [])
    r = _invoke("money-flow", "--purge-demo", "--yes")
    assert r.exit_code == 0, r.output
    assert "已删除: 599 行" in r.output
    assert calls == [{"dry_run": True}, {}]


def test_money_flow_cmd_demo_targets(monkeypatch):
    """--demo 不联网，只跑两个样板标的。"""
    seen = _stub_flow(monkeypatch)
    r = _invoke("money-flow", "--demo")
    assert r.exit_code == 0, r.output
    assert seen["targets"] == ["600519.SH", "000001.SZ"]
    assert seen["demo"] is True


def test_money_flow_cmd_progress_is_throttled(monkeypatch):
    """几千只标的不能逐只刷屏：按步长报，且最后一条必须报出来。"""
    def fake_backfill(targets, **kw):
        p = kw["progress"]
        p(1, 100, "600000.SH")      # 未到步长（100//50=2）→ 不打印
        p(2, 100, "600001.SH")      # 到步长 → 打印
        p(100, 100, "600099.SH")    # 收尾 → 必打印
        return dict(_FLOW_REPORT)

    _patch_mod(monkeypatch, "lquant.market.backfill", "backfill_money_flow_history",
               fake_backfill)
    _patch_mod(monkeypatch, "lquant.market.backfill", "money_flow_symbols",
               lambda **kw: [])
    r = _invoke("money-flow", "--symbols", "600519")
    assert r.exit_code == 0, r.output
    assert "[1/100]" not in r.output
    assert "[2/100] 600001.SH" in r.output
    assert "[100/100] 600099.SH" in r.output
