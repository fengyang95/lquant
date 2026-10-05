"""报告中心索引 + 陈旧报告维护（``lq factor reports``）。

对应评审 R15 的第二半：**失效标记 ≠ 重算**。能识别旧口径还不够，还要能
重算或清理，否则那批「修复前的错误报告」会永远挂在报告中心里被当结论读。
"""
from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")

from lquant.factors.evaluate import report as rep  # noqa: E402
from lquant.factors.evaluate import reports_index as idx  # noqa: E402


def _write(path, version: str | None = "2.0") -> None:
    """写一份最小报告：带/不带版本 meta（不带 = 修复前的产物）。"""
    meta = "" if version is None else (
        f'<meta name="lquant-report-generator" content="{version}">\n'
        '<meta name="lquant-report-generated-at" content="2026-02-01 09:30">\n')
    path.write_text(
        f'<!DOCTYPE html><html><head><meta charset="utf-8">\n{meta}'
        "<title>t</title></head><body>"
        "<dt>因子表达式</dt><dd>pct_change_5</dd></body></html>",
        encoding="utf-8")


# --------------------------------------------------------------------------- #
# 索引与陈旧判定
# --------------------------------------------------------------------------- #
def test_report_row_marks_missing_version_as_stale(tmp_path) -> None:
    cur, old = tmp_path / "cur.html", tmp_path / "old.html"
    _write(cur, rep.REPORT_GENERATOR_VERSION)
    _write(old, None)

    r_cur = idx.report_row(cur)
    assert r_cur["stale"] is False
    assert r_cur["generator_version"] == rep.REPORT_GENERATOR_VERSION
    assert r_cur["generated_at"] == "2026-02-01 09:30"
    assert r_cur["url"] == "/api/factors/reports/cur"

    r_old = idx.report_row(old)
    assert r_old["stale"] is True
    assert r_old["generator_version"] is None
    # 没有 meta 时回退到文件 mtime，而不是留空
    assert r_old["generated_at"]


def test_report_row_flags_older_generator_version(tmp_path) -> None:
    p = tmp_path / "v1.html"
    _write(p, "1.0")
    assert idx.report_row(p)["stale"] is True


def test_list_and_stale_reports(tmp_path) -> None:
    _write(tmp_path / "a.html", rep.REPORT_GENERATOR_VERSION)
    _write(tmp_path / "b.html", None)
    rows = idx.list_reports(tmp_path)
    assert {r["name"] for r in rows} == {"a", "b"}
    assert [r["name"] for r in idx.stale_reports(tmp_path)] == ["b"]
    assert idx.list_reports(tmp_path / "nope") == []


def test_read_report_expr(tmp_path) -> None:
    p = tmp_path / "x.html"
    _write(p)
    assert idx.read_report_expr(p) == "pct_change_5"
    p2 = tmp_path / "y.html"
    p2.write_text("<html><body>no expr</body></html>", encoding="utf-8")
    assert idx.read_report_expr(p2) is None


# --------------------------------------------------------------------------- #
# 表达式推断：只认精确来源，绝不猜
# --------------------------------------------------------------------------- #
def test_resolve_prefers_embedded_expr(tmp_path) -> None:
    p = tmp_path / "whatever.html"
    _write(p)
    expr, why = idx.resolve_report_expression("whatever", p)
    assert expr == "pct_change_5"
    assert "内嵌" in why


def test_resolve_falls_back_to_factor_def(tmp_path, monkeypatch) -> None:
    import lquant.core.db as db_mod

    class _Con:
        def execute(self, sql, params=None):
            return self

        def fetchone(self):
            return ("Ts_Mean($close,5)",)

    class _Ctx:
        def __enter__(self):
            return _Con()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(db_mod, "reader", lambda: _Ctx())
    expr, why = idx.resolve_report_expression("myfac")
    assert expr == "Ts_Mean($close,5)"
    assert "factor_def.name" in why


def test_resolve_reports_reason_when_unknown(tmp_path, monkeypatch) -> None:
    import lquant.core.db as db_mod

    class _Con:
        def execute(self, sql, params=None):
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _Ctx:
        def __enter__(self):
            return _Con()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(db_mod, "reader", lambda: _Ctx())
    expr, why = idx.resolve_report_expression("ws1790835001")
    assert expr is None
    assert "无法自动推断" in why


def test_resolve_canonical_id_reverse_lookup(tmp_path, monkeypatch) -> None:
    """`factor_<canonical_id>` 命名的报告（CLI 产物）要能反查到表达式。"""
    import lquant.core.db as db_mod
    from lquant.factors.dsl.printer import canonical_id

    expr_text = "Ts_Mean($close,5)"
    cid = canonical_id(expr_text)

    class _Con:
        def execute(self, sql, params=None):
            self._sql = sql
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            return [("myfac", expr_text)]

    class _Ctx:
        def __enter__(self):
            return _Con()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(db_mod, "reader", lambda: _Ctx())
    expr, why = idx.resolve_report_expression(f"factor_{cid}")
    assert expr == expr_text
    assert "canonical_id" in why


# --------------------------------------------------------------------------- #
# CLI：list / prune / rebuild
# --------------------------------------------------------------------------- #
@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    """把 CLI 与索引都指向同一个 tmp 报告目录。"""
    from lquant.core.config import get_settings
    from lquant.server.api import factors as api

    base = tmp_path / "reports"
    base.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "reports_dir", str(base), raising=False)
    monkeypatch.setattr(api, "REPORT_DIR", base)
    return base


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["factor", *args])


def test_cli_reports_lists_and_marks_stale(report_dir) -> None:
    import json

    _write(report_dir / "cur.html", rep.REPORT_GENERATOR_VERSION)
    _write(report_dir / "old.html", None)
    r = _invoke("reports")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_reports"] == 2
    assert body["scope"] == "all"
    stale = {x["name"]: x["stale"] for x in body["reports"]}
    assert stale == {"cur": False, "old": True}


def test_cli_reports_stale_only_and_prune_dry_run(report_dir) -> None:
    import json

    _write(report_dir / "cur.html", rep.REPORT_GENERATOR_VERSION)
    _write(report_dir / "old.html", None)
    r = _invoke("reports", "--prune-stale")
    body = json.loads(r.output)
    # 不加 --yes 只演练：不真删；且只针对旧口径（当前口径的不许被卷进来）
    assert body["scope"] == "stale"
    assert body["prune"]["dry_run"] is True
    assert body["prune"]["names"] == ["old"]
    assert (report_dir / "old.html").exists()

    r2 = _invoke("reports", "--prune-stale", "--yes")
    body2 = json.loads(r2.output)
    assert body2["prune"]["dry_run"] is False
    assert body2["prune"]["deleted"] == 1
    assert not (report_dir / "old.html").exists()
    assert (report_dir / "cur.html").exists()


def test_cli_reports_rebuild_stale(report_dir, monkeypatch) -> None:
    """能推断出表达式的旧报告 → 重算；推断不出的 → 跳过并给原因。"""
    import json

    from lquant.cli.commands import factor as cli_factor

    _write(report_dir / "resolvable.html", None)     # 内嵌 pct_change_5
    (report_dir / "mystery.html").write_text(
        "<html><body>旧报告，无表达式、名字也不是已注册因子</body></html>",
        encoding="utf-8")

    calls = []

    def _fake_build(expr, *, out=None, start=None, **kw):
        calls.append((expr, out))
        _write(__import__("pathlib").Path(out), rep.REPORT_GENERATOR_VERSION)
        return {"report": out, "bytes": 1}

    monkeypatch.setattr(cli_factor, "_build_report", _fake_build)
    monkeypatch.setattr(idx, "resolve_report_expression",
                        lambda stem, path=None: (
                            ("pct_change_5", "报告内嵌表达式") if stem == "resolvable"
                            else (None, "无法自动推断")))
    monkeypatch.setattr(
        "lquant.factors.evaluate.reports_index.resolve_report_expression",
        idx.resolve_report_expression)

    r = _invoke("reports", "--rebuild-stale")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["scope"] == "stale"
    assert [x["name"] for x in body["rebuilt"]] == ["resolvable"]
    assert calls and calls[0][0] == "pct_change_5"
    assert calls[0][1].endswith("resolvable.html")   # 覆盖同名文件，不改名
    assert [x["name"] for x in body["skipped"]] == ["mystery"]
    assert "无法自动推断" in body["skipped"][0]["reason"]
    # 重算后不再是旧口径
    assert idx.report_row(report_dir / "resolvable.html")["stale"] is False


def test_cli_reports_rebuild_does_not_touch_current_reports(report_dir, monkeypatch) -> None:
    """回归：--rebuild-stale 只能碰旧口径报告。

    首版实现遍历的是「全部报告」，于是会把已经是最新口径的报告也拿去重算 ——
    既是纯浪费，还可能用不同的数据窗口把它改掉。
    """
    import json

    from lquant.cli.commands import factor as cli_factor

    _write(report_dir / "current.html", rep.REPORT_GENERATOR_VERSION)
    _write(report_dir / "old.html", None)

    calls = []

    def _fake_build(expr, *, out=None, start=None, **kw):
        calls.append(out)
        return {"report": out, "bytes": 1}

    monkeypatch.setattr(cli_factor, "_build_report", _fake_build)
    monkeypatch.setattr("lquant.factors.evaluate.reports_index.resolve_report_expression",
                        lambda stem, path=None: ("pct_change_5", "stub"))
    r = _invoke("reports", "--rebuild-stale")
    body = json.loads(r.output)
    assert [x["name"] for x in body["rebuilt"]] == ["old"]
    assert calls == [str(report_dir / "old.html")]
    assert body["n_reports"] == 2 and body["n_selected"] == 1
