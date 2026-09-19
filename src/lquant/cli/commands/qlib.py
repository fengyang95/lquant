"""lq qlib：日线湖导出 qlib 二进制数据 + 驱动 qlib 工作流（因子分析/回测）。

pyqlib 不进主 venv（依赖树重）。`lq qlib workflow` 的探测顺序：
1. 当前解释器能 import qlib → 进程内直接跑；
2. LQ_QLIB_PYTHON 环境变量指向的 venv python → 子进程跑；
3. --python 显式指定 → 子进程跑；都不可用则报错并给安装提示。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from lquant.qlib_io.export import check, export


@click.group()
def qlib() -> None:
    """qlib 接入：export 导出数据 / check 自检 / workflow 跑因子分析与回测"""


@qlib.command("export")
@click.option("--out", default="data/qlib", show_default=True, help="qlib 数据根目录（provider_uri）")
@click.option("--start", default=None, help="起始日期 YYYY-MM-DD（含）")
@click.option("--end", default=None, help="结束日期 YYYY-MM-DD（含）")
@click.option("--symbol", "symbols", multiple=True, help="标的白名单（可多次，如 600000.SH）")
@click.option("--sec-type", "sec_types", multiple=True, default=("stock",), show_default=True,
              help="sec_type 过滤（可多次）")
@click.option("--field", "fields", multiple=True, help="导出字段（可多次；缺省为默认全集）")
@click.option("--top", default=None, type=int, help="额外产出 instruments/topN.txt（按末日 float_mv）")
def export_cmd(out: str, start, end, symbols, sec_types, fields, top) -> None:
    """把日线湖导出为 qlib 二进制数据（calendars/instruments/features）。"""
    manifest = export(
        out_dir=out,
        start=start,
        end=end,
        symbols=list(symbols) or None,
        sec_types=list(sec_types) or None,
        fields=list(fields) or None,
        top=top,
    )
    click.echo(json.dumps(manifest, ensure_ascii=False, indent=2))


@qlib.command("check")
@click.option("--dir", "out_dir", default="data/qlib", show_default=True)
def check_cmd(out_dir: str) -> None:
    """导出结果结构自检（日历/清单/features 一致性 + bin 抽样回读）。"""
    r = check(out_dir)
    click.echo(json.dumps(r, ensure_ascii=False, indent=2))
    if r["problems"]:
        sys.exit(1)


def _find_qlib_python(python: str | None) -> str | None:
    """定位可用的 qlib 解释器；None = 当前解释器即可。"""
    if python:
        return python
    env_py = os.environ.get("LQ_QLIB_PYTHON")
    if env_py and Path(env_py).exists():
        return env_py
    try:
        import qlib  # noqa: F401

        return None
    except ImportError:
        pass
    for cand in (".venv-qlib/bin/python", ".venv-qlib/Scripts/python.exe"):
        p = Path(cand)
        if p.exists():
            return str(p)
    return ""


@qlib.command()
@click.option("--config", "config_path", default="config/qlib/workflow_alpha158_lgbm.yaml",
              show_default=True, help="qrun 风格 workflow yaml")
@click.option("--provider", default=None, help="qlib 数据根目录（覆盖 config；缺省 data/qlib）")
@click.option("--python", "qlib_python", default=None, help="pyqlib 所在 venv 的 python 路径")
@click.option("--market", default=None, help="覆盖股票池（instruments 文件名，如 all / top300）")
@click.option("--exp-name", default="lquant_qlib", show_default=True)
@click.option("--out", default="data/qlib/last_metrics.json", show_default=True, help="metrics JSON 输出")
def workflow(config_path: str, provider, qlib_python, market, exp_name: str, out: str) -> None:
    """跑 qlib 工作流：训练 → IC/ICIR/RankIC → 组合回测（config 含 port_analysis 时）。"""
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise SystemExit(f"workflow 配置不存在：{cfg_path}")
    provider = provider or "data/qlib"
    if not Path(provider).exists():
        raise SystemExit(f"qlib 数据目录不存在：{provider}（先跑 lq qlib export）")
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    resolved = _find_qlib_python(qlib_python)
    if resolved is None:
        # 当前解释器自带 qlib：进程内跑
        from lquant.qlib_io import runner as _runner

        argv = ["--provider", provider, "--config", str(cfg_path),
                "--exp-name", exp_name, "--out", str(out_p)]
        if market:
            argv += ["--market", market]
        rc = _runner.main(argv)
    elif resolved:
        runner_py = Path(__file__).resolve().parents[2] / "qlib_io" / "runner.py"
        cmd = [resolved, str(runner_py), "--provider", provider, "--config", str(cfg_path),
               "--exp-name", exp_name, "--out", str(out_p)]
        if market:
            cmd += ["--market", market]
        click.echo(f"[qlib] 子进程运行：{cmd}")
        rc = subprocess.run(cmd).returncode
    else:
        raise SystemExit(
            "当前环境没有 pyqlib。安装专用 venv：\n"
            "  python -m venv .venv-qlib\n"
            "  .venv-qlib/bin/pip install pyqlib lightgbm\n"
            "或用 --python / LQ_QLIB_PYTHON 指定已有解释器。"
        )
    if rc != 0:
        sys.exit(rc)
    if out_p.exists():
        metrics = json.loads(out_p.read_text(encoding="utf-8"))
        click.echo("[qlib] 关键指标：")
        for m in ("IC", "ICIR", "Rank IC", "Rank ICIR",
                  "excess_return_without_cost", "excess_return_with_cost"):
            if m in metrics:
                click.echo(f"  {m}: {metrics[m]}")
        click.echo(f"完整指标已写入 {out_p}")
