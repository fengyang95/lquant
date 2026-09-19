"""qlib 接入层单测：symbol 映射、湖→bin 导出语义、结构自检、CLI 接线。

导出语义回归点：
- 价格复权：导出 OHLC = raw × adj_factor/adj_factor_latest（前复权到最新），
  $factor 记录归一化因子； Alpha158 依赖的 $vwap = amount/volume（复权），
  停牌（volume<=0）置 NaN；
- bin 对齐：值按日历下标落位，标的中间缺失日为 NaN（不是压缩序列）。
"""
from __future__ import annotations

import sys
import types
from datetime import date

import numpy as np
import polars as pl
import pytest

from lquant.data.schema import SCHEMAS
from lquant.data.store.parquet import write_daily
from lquant.qlib_io.export import (
    _read_bin,
    check,
    export,
    from_qlib_symbol,
    to_qlib_symbol,
)

DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]


@pytest.fixture()
def lake_root(tmp_path, monkeypatch):
    """把 parquet 湖指向 tmp：patch 模块级 get_settings（parquet_dir 缺省是
    相对 CWD 的路径，LQ_ROOT 只影响 find_root，环境变量隔离不可靠）。"""
    import lquant.data.store.parquet as pq_store

    class _S:
        parquet_dir = str(tmp_path / "parquet")

    monkeypatch.setattr(pq_store, "get_settings", lambda: _S())
    return tmp_path


def _bar(sym: str, d: date, close: float, adj: float = 1.0,
         volume: float = 1e6, amount: float | None = None) -> pl.DataFrame:
    amt = amount if amount is not None else close * volume
    part = pl.DataFrame(
        {
            "symbol": [sym],
            "trade_date": [d],
            "open": [close * 0.99],
            "high": [close * 1.01],
            "low": [close * 0.98],
            "close": [close],
            "volume": [volume],
            "amount": [amt],
            "adj_factor": [adj],
            "sec_type": ["stock"],
        }
    )
    rest = {c: dt for c, dt in dict(SCHEMAS["daily_bar"]).items() if c not in part.columns}
    return pl.concat([part, pl.DataFrame(schema=rest)], how="diagonal")


@pytest.fixture()
def mini_lake(lake_root):
    """A：4 天、adj_factor 中途 1→1.1；B：缺 1/4（内部空洞）、因子恒 1。"""
    rows = []
    for i, d in enumerate(DATES):
        rows.append(_bar("600000.SH", d, close=10.0 + i, adj=1.1 if i >= 2 else 1.0))
    for i, d in enumerate(DATES):
        if d == date(2024, 1, 4):
            continue
        rows.append(_bar("000001.SZ", d, close=20.0 + i))
    write_daily(pl.concat(rows))
    return lake_root


# ---------------------------------------------------------------- symbol 映射

@pytest.mark.parametrize(("src", "dst"), [
    ("600000.SH", "SH600000"),
    ("000001.SZ", "SZ000001"),
    ("430047.BJ", "BJ430047"),
])
def test_symbol_roundtrip(src, dst):
    assert to_qlib_symbol(src) == dst
    assert from_qlib_symbol(dst) == src


@pytest.mark.parametrize("bad", ["600000", "600000.HK", ".SH", "SH600000"])
def test_symbol_reject(bad):
    with pytest.raises(ValueError):
        to_qlib_symbol(bad)


@pytest.mark.parametrize("bad", ["600000", "XX600000", "SH"])
def test_qlib_symbol_reject(bad):
    with pytest.raises(ValueError):
        from_qlib_symbol(bad)


# ---------------------------------------------------------------- 导出语义

def test_export_basic(mini_lake, tmp_path):
    out = tmp_path / "qdata"
    manifest = export(out, start="2024-01-01", end="2024-12-31")
    assert manifest["instruments"] == 2
    assert manifest["calendar_days"] == 4
    assert manifest["fields"] == list(
        ("open", "high", "low", "close", "volume", "amount", "vwap", "factor"))

    days = (out / "calendars" / "day.txt").read_text().split()
    assert days == [d.strftime("%Y-%m-%d") for d in DATES]

    ins = (out / "instruments" / "all.txt").read_text().strip().splitlines()
    assert ins == [
        "SH600000\t2024-01-02\t2024-01-05",
        "SZ000001\t2024-01-02\t2024-01-05",
    ]


def test_export_adjusted_close_and_factor(mini_lake, tmp_path):
    """600000.SH：前两日 factor=1、后两日 1.1 → 复权 close = raw×f/f_last，
    f_last=1.1；最新一日复权价=raw。$factor 记录 f/f_last。"""
    out = tmp_path / "qdata"
    export(out)
    start_i, close = _read_bin(out / "features" / "SH600000" / "close.day.bin")
    start_f, factor = _read_bin(out / "features" / "SH600000" / "factor.day.bin")
    assert start_i == 0 and start_f == 0
    raw = [10.0, 11.0, 12.0, 13.0]
    adj = [r * (1.1 if i >= 2 else 1.0) / 1.1 for i, r in enumerate(raw)]
    np.testing.assert_allclose(close, adj, rtol=1e-6)
    np.testing.assert_allclose(factor, [1 / 1.1, 1 / 1.1, 1.0, 1.0], rtol=1e-6)


def test_export_interior_gap_is_nan(mini_lake, tmp_path):
    """000001.SZ 缺 1/4：bin 长度覆盖全日历，缺失位是 NaN 而非压缩。"""
    out = tmp_path / "qdata"
    export(out)
    start_i, close = _read_bin(out / "features" / "SZ000001" / "close.day.bin")
    assert start_i == 0 and close.size == 4
    assert np.isnan(close[2])
    np.testing.assert_allclose(close[[0, 1, 3]], [20.0, 21.0, 23.0], rtol=1e-6)


def test_export_vwap_suspended_nan(mini_lake, tmp_path):
    """volume=0（停牌语义）→ $vwap 为 NaN；正常日 vwap=amount/volume×f。"""
    rows = [_bar("600519.SH", d, close=100.0, volume=0.0 if d == DATES[1] else 2.0)
            for d in DATES]
    write_daily(pl.concat(rows))
    out = tmp_path / "qdata"
    export(out)
    _, vwap = _read_bin(out / "features" / "SH600519" / "vwap.day.bin")
    assert np.isnan(vwap[1])
    np.testing.assert_allclose(vwap[0], 100.0, rtol=1e-6)


def test_export_optional_fields_and_top(mini_lake, tmp_path):
    rows = []
    for i, d in enumerate(DATES):
        part = _bar("600000.SH", d, close=10.0 + i)
        rows.append(part.with_columns(
            pl.lit(8e10 + i, dtype=pl.Float64).alias("float_mv"),
            pl.lit(10.0 + i, dtype=pl.Float64).alias("pe_ttm"),
        ))
    for d in DATES:
        part = _bar("000001.SZ", d, close=20.0)
        rows.append(part.with_columns(
            pl.lit(2e11, dtype=pl.Float64).alias("float_mv"),
            pl.lit(5.0, dtype=pl.Float64).alias("pe_ttm"),
        ))
    write_daily(pl.concat(rows))
    out = tmp_path / "qdata"
    manifest = export(out, fields=["close", "pe_ttm", "vwap"], top=1)
    assert manifest["fields"] == ["close", "pe_ttm", "vwap"]
    assert (out / "features" / "SH600000" / "pe_ttm.day.bin").exists()
    assert not (out / "features" / "SH600000" / "open.day.bin").exists()
    top = (out / "instruments" / "top1.txt").read_text().strip()
    assert top.startswith("SZ000001\t")  # float_mv 更大者入选


def test_export_rejects_unknown_field(mini_lake, tmp_path):
    with pytest.raises(ValueError, match="未知字段"):
        export(tmp_path / "q", fields=["nope"])


def test_export_empty_lake_raises(lake_root, tmp_path):
    with pytest.raises(ValueError, match="无可导出"):
        export(tmp_path / "q")


def test_export_sec_type_filter(mini_lake, tmp_path):
    """湖里混入 etf 行时默认被过滤（sec_types 缺省 stock）。"""
    rows = [_bar("510300.SH", d, close=3.0) for d in DATES]
    rows = [r.with_columns(pl.lit("etf").alias("sec_type")) for r in rows]
    write_daily(pl.concat(rows))
    out = tmp_path / "qdata"
    manifest = export(out)
    assert manifest["instruments"] == 2  # 只有 stock
    manifest2 = export(out, sec_types=["stock", "etf"])
    assert manifest2["instruments"] == 3


# ---------------------------------------------------------------- 自检 / CLI

def test_check_ok_and_detects_damage(mini_lake, tmp_path):
    out = tmp_path / "qdata"
    export(out)
    r = check(out)
    assert r["problems"] == []
    assert r["instruments"] == 2 and r["days"] == 4
    import shutil

    shutil.rmtree(out / "features" / "SZ000001")
    r2 = check(out)
    assert any("features" in p for p in r2["problems"])


def test_cli_export_and_check(mini_lake, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from lquant.cli.commands.qlib import qlib as qlib_grp

    monkeypatch.chdir(tmp_path)
    out = str(tmp_path / "qdata")
    runner = CliRunner()
    r = runner.invoke(qlib_grp, ["export", "--out", out, "--field", "close"])
    assert r.exit_code == 0, r.output
    assert '"instruments": 2' in r.output
    r2 = runner.invoke(qlib_grp, ["check", "--dir", out])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(qlib_grp, ["export", "--out", out, "--field", "nope"])
    assert r3.exit_code != 0


# ---------------------------------------------------------------- runner / workflow CLI（mock qlib）

def _fake_qlib_modules(monkeypatch):
    from unittest.mock import MagicMock

    fake_qlib = MagicMock()
    fake_qlib.utils.init_instance_by_config.side_effect = lambda cfg: MagicMock(name=cfg.get("class", "fake"))
    metrics = {"IC": 0.05, "ICIR": 0.4}

    fake_R = MagicMock()
    fake_R.get_recorder.return_value.list_metrics.return_value = metrics
    fake_qlib.workflow.R = fake_R
    for name, mod in {
        "qlib": fake_qlib,
        "qlib.utils": fake_qlib.utils,
        "qlib.workflow": fake_qlib.workflow,
        "qlib.workflow.record_temp": fake_qlib.workflow.record_temp,
        "qlib.config": fake_qlib.config,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return fake_qlib, metrics


def test_runner_main_mocked(tmp_path, monkeypatch):
    """runner 主链路：不依赖真 qlib，验证 yaml→init→fit→records→metrics JSON。"""
    import json as _json

    cfg = {
        "qlib_init": {"provider_uri": "data/qlib", "region": "cn"},
        "market": "all",
        "dataset": {"kwargs": {"handler": {"class": "Alpha158"}}},
        "model": {"class": "LGBMModel"},
        "port_analysis": {"strategy": {}, "backtest": {}},
    }
    cfg_p = tmp_path / "wf.yaml"
    cfg_p.write_text(_json.dumps(cfg), encoding="utf-8")
    out_p = tmp_path / "metrics.json"

    import lquant.qlib_io.runner as runner_mod

    fake_qlib, metrics = _fake_qlib_modules(monkeypatch)
    rc = runner_mod.main([
        "--provider", "data/qlib", "--config", str(cfg_p),
        "--exp-name", "ut", "--out", str(out_p),
    ])
    assert rc == 0
    fake_qlib.init.assert_called_once_with(provider_uri="data/qlib", region="cn")
    assert fake_qlib.workflow.record_temp.PortAnaRecord.called  # 回测段被执行
    assert _json.loads(out_p.read_text()) == metrics


def test_runner_requires_provider(tmp_path, monkeypatch):
    import pytest

    import lquant.qlib_io.runner as runner_mod

    _fake_qlib_modules(monkeypatch)
    cfg_p = tmp_path / "wf.yaml"
    cfg_p.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="provider_uri"):
        runner_mod.main(["--config", str(cfg_p)])


def test_cli_workflow_subprocess(tmp_path, monkeypatch):
    """主 venv 无 qlib 时走子进程：验证 cmd 拼装与 metrics 展示。"""
    import json as _json

    from click.testing import CliRunner

    from lquant.cli.commands import qlib as qlib_cmd_mod
    from lquant.cli.commands.qlib import qlib as qlib_grp

    cfg_p = tmp_path / "wf.yaml"
    cfg_p.write_text("qlib_init: {}\n", encoding="utf-8")
    provider = tmp_path / "qdata"
    provider.mkdir()
    out_p = tmp_path / "m.json"

    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        out_p.write_text(_json.dumps({"IC": 0.03}), encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(qlib_cmd_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(qlib_cmd_mod, "_find_qlib_python", lambda p: "/fake/py")
    r = CliRunner().invoke(qlib_grp, [
        "workflow", "--config", str(cfg_p), "--provider", str(provider),
        "--out", str(out_p), "--market", "top300",
    ])
    assert r.exit_code == 0, r.output
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/py" and cmd[1].endswith("runner.py")
    assert "--market" in cmd and cmd[cmd.index("--market") + 1] == "top300"
    assert "IC: 0.03" in r.output


def test_cli_workflow_in_process(tmp_path, monkeypatch):
    """当前解释器有 qlib（mock 掉 import）→ 进程内调用 runner.main。"""
    from click.testing import CliRunner

    from lquant.cli.commands import qlib as qlib_cmd_mod
    from lquant.cli.commands.qlib import qlib as qlib_grp

    cfg_p = tmp_path / "wf.yaml"
    cfg_p.write_text("qlib_init: {}\n", encoding="utf-8")
    provider = tmp_path / "qdata"
    provider.mkdir()

    calls = {}
    monkeypatch.setattr(qlib_cmd_mod, "_find_qlib_python", lambda p: None)
    monkeypatch.setattr("lquant.qlib_io.runner.main", lambda argv: calls.update(argv=argv) or 0)
    r = CliRunner().invoke(qlib_grp, ["workflow", "--config", str(cfg_p), "--provider", str(provider)])
    assert r.exit_code == 0, r.output
    assert "--provider" in calls["argv"]


def test_cli_workflow_errors(tmp_path):
    from click.testing import CliRunner

    from lquant.cli.commands.qlib import qlib as qlib_grp

    r = CliRunner().invoke(qlib_grp, ["workflow", "--config", str(tmp_path / "nope.yaml")])
    assert r.exit_code != 0 and "不存在" in r.output
    cfg_p = tmp_path / "wf.yaml"
    cfg_p.write_text("qlib_init: {}\n", encoding="utf-8")
    r2 = CliRunner().invoke(qlib_grp, ["workflow", "--config", str(cfg_p), "--provider", str(tmp_path / "missing")])
    assert r2.exit_code != 0 and "先跑" in r2.output


def test_find_qlib_python_env(tmp_path, monkeypatch):
    from lquant.cli.commands.qlib import _find_qlib_python

    py = tmp_path / "py"
    py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("LQ_QLIB_PYTHON", str(py))
    assert _find_qlib_python(None) == str(py)
    monkeypatch.delenv("LQ_QLIB_PYTHON")
    monkeypatch.setitem(sys.modules, "qlib", types.ModuleType("qlib"))
    assert _find_qlib_python(None) is None  # 当前解释器可用 → 进程内
    monkeypatch.delitem(sys.modules, "qlib")
    assert _find_qlib_python(None) == ""  # 找不到 → 提示安装
