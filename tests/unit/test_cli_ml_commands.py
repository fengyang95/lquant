"""`lq ml` CLI 回归测试（CLI 是 Agent 的运维面，此前几乎零覆盖）。

覆盖：status / features / train / retrain / models / promote / rollback /
production / events / predict / signals，以及各自的失败退出路径。

CLI 与 API 共用同一套 ``research.ml`` 实现 —— 这里只断言「命令能跑通、
输出可解析、失败有明确退出码」，数值正确性是 test_ml_* 的职责。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("cli_env")


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("cli_ml")
    prev_cwd = os.getcwd()
    os.chdir(base)
    # run_ml_pipeline 会顺带跑回测引擎，引擎要读 config/rules/cn_a_share.yaml
    shutil.copytree(Path(__file__).resolve().parents[2] / "config",
                    base / "config", dirs_exist_ok=True)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_ml_run_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_ml_run_columns(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["ml", *args])


def _train(name: str = "cli_line", **kw):
    args = ["train", "--features", "MA20", "--train-end", "2025-12-31",
            "--valid-end", "2026-03-31", "--name", name, "--kind", "ridge",
            "--top-n", "3", "--start", "2024-01-01", "--end", "2026-06-30"]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    r = _invoke(*args)
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_cli_status_and_features(cli_env):
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    assert "模型目录" in r.output and "后端" in r.output

    r2 = _invoke("features", "--limit", "5")
    assert r2.exit_code == 0, r2.output
    assert "Alpha158 内置: 158" in r2.output
    # 指数成分未同步时退回内置清单（而不是让命令失败）
    r3 = _invoke("features", "--universe", "hs300")
    assert r3.exit_code == 0, r3.output
    assert "简单公式" in r3.output


def test_cli_train_registers_version_then_manage(cli_env):
    out = _train()
    assert out["model"]["name"] == "cli_line"
    assert out["model"]["version"] == 1

    r = _invoke("models", "--name", "cli_line")
    assert r.exit_code == 0, r.output
    assert "cli_line v1" in r.output
    assert _invoke("models", "--name", "nope").output.strip() == "（无匹配版本）"
    assert _invoke("models", "--stage", "candidate").exit_code == 0

    # 训练第二个版本：晋级新版本时旧线上版自动 archived，回滚才有目标
    out2 = _train()
    assert out2["model"]["version"] == 2

    p = _invoke("promote", "cli_line", "1", "--note", "首次上线")
    assert p.exit_code == 0, p.output
    assert json.loads(p.output)["stage"] == "production"
    p2 = _invoke("promote", "cli_line", "2")
    assert p2.exit_code == 0, p2.output

    prod = _invoke("production", "cli_line")
    assert prod.exit_code == 0, prod.output
    assert json.loads(prod.output)["version"] == 2
    # as-of 走事件流重放
    asof = _invoke("production", "cli_line", "--asof", "2099-01-01T00:00:00")
    assert asof.exit_code == 0, asof.output
    assert json.loads(asof.output)["version"] == 2

    rb = _invoke("rollback", "cli_line")
    assert rb.exit_code == 0, rb.output
    assert json.loads(rb.output)["version"] == 1

    ev = _invoke("events", "cli_line")
    assert ev.exit_code == 0, ev.output
    assert any(e["to_stage"] == "production" for e in json.loads(ev.output))


def test_cli_model_line_errors(cli_env):
    assert _invoke("production", "nope").exit_code != 0
    assert _invoke("rollback", "nope").exit_code != 0
    assert _invoke("production", "nope", "--asof", "2026-01-01T00:00:00").exit_code != 0
    # 空特征清单要在参数层挡掉，而不是跑到训练里才炸
    r = _invoke("train", "--features", "", "--train-end", "2025-12-31",
                "--valid-end", "2026-03-31", "--name", "x")
    assert r.exit_code != 0


def test_cli_predict_and_signals(cli_env):
    _train("cli_pred")
    _invoke("promote", "cli_pred", "1")
    last = "2026-06-30"
    r = _invoke("predict", "--name", "cli_pred", "--features", "MA20",
                "--date", last, "--start", "2026-01-01", "--end", last)
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["rows"] > 0 and body["version"] == 1

    sig = _invoke("signals", "--name", "cli_pred", "--limit", "3")
    assert sig.exit_code == 0, sig.output
    assert "行，版本" in sig.output

    # 没有线上版本 → 明确失败（而不是静默给空信号）
    assert _invoke("predict", "--name", "nope", "--features", "MA20").exit_code != 0
    # 湖空 → SystemExit 提示先取数
    assert _invoke("predict", "--name", "cli_pred", "--features", "MA20",
                   "--start", "1990-01-01", "--end", "1990-01-02").exit_code != 0


def test_cli_retrain_rolls_windows(cli_env):
    r = _invoke("retrain", "--features", "MA20", "--name", "cli_roll",
                "--start", "2024-01-01", "--end", "2026-06-30",
                "--kind", "ridge", "--top-n", "3", "--train-months", "6",
                "--valid-months", "2", "--test-months", "2", "--step-months", "3",
                "--no-promote")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["windows"] >= 1
    assert out["n_promoted"] == 0
