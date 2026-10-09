"""``lq ext`` CLI 的端到端与错误路径（覆盖率补齐）。

隔离姿势沿用 test_board_features：LQ_ROOT 指 tmp + chdir + cache_clear +
全量 DDL。全程离线：HTTP 回补用 monkeypatch 换掉 ``ext.backfill``，
绝不真的发请求。
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lquant.cli.commands.ext import ext


@pytest.fixture
def ext_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    yield tmp_path
    get_settings.cache_clear()


def _run(*args, **kw):
    return CliRunner().invoke(ext, list(args), **kw)


def _create(env, table_id="heat", *extra):
    return _run(
        "create", table_id, "--label", "热度", "--mode", "timeseries",
        "--field", "heat:float", "--field", "concepts:string", *extra,
    )


def test_ext_list_empty_and_create_and_list(ext_env) -> None:
    r = _run("list")
    assert r.exit_code == 0 and "还没有扩展表" in r.output  # 空列表分支

    r = _create(ext_env)
    assert r.exit_code == 0, r.output
    assert "已创建扩展表 heat" in r.output

    r = _run("list")
    assert r.exit_code == 0 and "热度" in r.output
    assert "个分区" in r.output  # timeseries 覆盖展示（无数据分支）


def test_ext_create_snapshot_and_market_level_list_coverage(ext_env) -> None:
    r = _run("create", "senti", "--label", "情绪", "--mode", "snapshot",
             "--field", "score:float")
    assert r.exit_code == 0, r.output
    csv = ext_env / "s.csv"
    csv.write_text("symbol,score\n600000,1.0\n", encoding="utf-8")
    assert _run("import", "senti", str(csv)).exit_code == 0

    r = _run("create", "mkt", "--label", "全市场", "--mode", "timeseries",
             "--field", "score:float", "--market-level", "--symbol-field", "")
    assert r.exit_code == 0, r.output
    r = _run("list")
    assert r.exit_code == 0 and "有快照" in r.output and "是" in r.output  # 市场级列


def test_ext_create_error_paths(ext_env) -> None:
    r = _run("create", "x", "--label", "x", "--mode", "timeseries",
             "--field", ":float")
    assert r.exit_code != 0 and "缺少字段名" in r.output

    r = _run("create", "x", "--label", "x", "--mode", "timeseries",
             "--field", "a:float", "--pull-url", "https://e.test",
             "--pull-header", "=只有值")
    assert r.exit_code != 0 and "KEY=VALUE" in r.output

    r = _run("create", "x", "--label", "x", "--mode", "timeseries",
             "--field", "a:float", "--pull-url", "https://e.test",
             "--pull-field-map", "只有键")
    assert r.exit_code != 0 and "外部名=内部名" in r.output

    r = _run("create", "../escape", "--label", "x", "--mode", "timeseries",
             "--field", "a:float")
    assert r.exit_code != 0  # ExtConfigError → ClickException


def test_ext_import_rows_values_and_bad_date(ext_env) -> None:
    assert _create(ext_env).exit_code == 0
    csv = ext_env / "u.csv"
    csv.write_text("symbol,heat,concepts\n600000,1.5,AI\n000001,2.5,银行\n",
                   encoding="utf-8")
    r = _run("import", "heat", str(csv), "--date", "2024-03-01")
    assert r.exit_code == 0 and "已导入 2 行" in r.output

    r = _run("rows", "heat", "--date", "2024-03-01", "--filter", "concepts:AI")
    assert r.exit_code == 0 and "热" in r.output or "heat" in r.output
    r = _run("rows", "heat", "--date", "2024-03-01", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["total"] == 2

    r = _run("rows", "heat", "--date", "2024-04-01")
    assert r.exit_code == 0 and "无数据" in r.output
    r = _run("rows", "heat", "--date", "不是日期")
    assert r.exit_code != 0 and "日期格式错误" in r.output
    r = _run("rows", "heat", "--filter", "ghost:1")
    assert r.exit_code != 0  # ExtConfigError → ClickException

    r = _run("values", "heat", "--field", "concepts", "--date", "2024-03-01")
    assert r.exit_code == 0 and "AI" in r.output
    r = _run("values", "heat", "--field", "ghost", "--date", "2024-03-01")
    assert r.exit_code != 0


def test_ext_load_missing_table(ext_env) -> None:
    for args in (("rows", "ghost"), ("values", "ghost", "--field", "x"),
                 ("delete", "ghost", "--yes")):
        r = _run(*args)
        assert r.exit_code != 0 and "不存在" in r.output


def test_ext_import_wrong_mode_and_import_error(ext_env) -> None:
    assert _create(ext_env).exit_code == 0
    csv = ext_env / "u.csv"
    csv.write_text("symbol,heat,concepts\n600000,1.5,AI\n", encoding="utf-8")
    r = _run("import", "heat", str(csv))  # timeseries 缺 --date
    assert r.exit_code != 0


def test_ext_backfill_report_and_failure(ext_env, monkeypatch) -> None:
    assert _create(ext_env, "bf", "--date-param", "date",
                   "--pull-url", "https://e.test/h").exit_code == 0
    import lquant.cli.commands.ext as ext_mod

    monkeypatch.setattr(ext_mod, "backfill", lambda *a, **k: {
        "total_days": 2, "fetched": 2, "skipped_existing": 0, "empty": 0,
        "failed": [], "rows_written": 4,
    })
    r = _run("backfill", "bf", "--start", "2024-03-01", "--end", "2024-03-04")
    assert r.exit_code == 0 and json.loads(r.output)["fetched"] == 2

    monkeypatch.setattr(ext_mod, "backfill", lambda *a, **k: {
        "total_days": 1, "fetched": 0, "skipped_existing": 0, "empty": 0,
        "failed": [{"date": "2024-03-04", "reason": "上游 500"}], "rows_written": 0,
    })
    r = _run("backfill", "bf", "--start", "2024-03-01", "--end", "2024-03-04")
    assert r.exit_code != 0 and "回补失败" in r.output


def test_ext_sync_factors_delete_confirm(ext_env) -> None:
    assert _create(ext_env).exit_code == 0
    r = _run("sync-factors")
    assert r.exit_code == 0 and "factors" in r.output

    r = _run("delete", "heat", input="n\n")  # 确认框选否 → abort
    assert r.exit_code != 0
    r = _run("delete", "heat", "--yes")
    assert r.exit_code == 0 and "已删除" in r.output
    assert _run("list").output.find("还没有扩展表") >= 0


def test_ext_pull_param_date_format_roundtrip(ext_env) -> None:
    r = _run("create", "cmp", "--label", "x", "--mode", "timeseries",
             "--field", "a:float", "--date-param", "d", "--date-format", "compact")
    assert r.exit_code == 0
    from lquant.data.ext.store import ExtConfigStore

    cfg = ExtConfigStore().get("cmp")
    assert cfg.date_param == "d" and cfg.date_format == "compact"


def test_ext_snapshot_snapshot_path_exists(ext_env) -> None:
    r = _run("create", "s2", "--label", "x", "--mode", "snapshot",
             "--field", "score:float")
    assert r.exit_code == 0
    from lquant.data.ext.storage import write_frame
    from lquant.data.ext.store import ExtConfigStore

    cfg = ExtConfigStore().get("s2")
    import polars as pl

    write_frame(pl.DataFrame({"symbol": ["600000.SH"], "score": [1.0]}), cfg)
    r = _run("list")
    assert "有快照" in r.output
