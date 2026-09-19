#!/usr/bin/env python
"""qlib 工作流 runner —— 在 qlib 专用 venv 内独立运行，不 import lquant。

用法（由 `lq qlib workflow` 自动拼装，也可手工执行）：

    <qlib-venv-python> runner.py \
        --provider /path/to/qlib_data \
        --config workflow.yaml \
        --exp-name lquant_qlib \
        --out metrics.json

config 采用 qlib 官方 qrun 的 workflow 配置格式（参考
examples/benchmarks_LGBM/workflow_config_lightgbm_Alpha158.yaml）：

    qlib_init: {provider_uri: ..., region: cn}
    market: all
    data_handler_config: {...}
    dataset: {...}      # DatasetH
    model: {...}        # 任意 qlib Model
    port_analysis:      # 可选；缺省跳过回测只做信号分析
      strategy: {...}
      backtest: {...}

执行内容 = qrun 主链路：model.fit(dataset) → SignalRecord（预测）→
SigAnaRecord（IC/ICIR/RankIC）→ PortAnaRecord（组合回测，可选），
结束后把 recorder metrics 写为 JSON（--out）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 新版 mlflow 把 filesystem tracking 后端改为显式 opt-in，否则 qlib 的
# R.start()（默认 file:./mlruns）直接抛 MlflowException。
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="qlib workflow runner (qrun-like)")
    ap.add_argument("--provider", default=None, help="qlib 数据根目录（覆盖 config.qlib_init.provider_uri）")
    ap.add_argument("--config", required=True, help="workflow yaml 路径")
    ap.add_argument("--exp-name", default="lquant_qlib")
    ap.add_argument("--market", default=None, help="覆盖 market/instruments（如 top300）")
    ap.add_argument("--recorder-name", default="runner")
    ap.add_argument("--out", default=None, help="metrics JSON 输出路径")
    args = ap.parse_args(argv)

    import yaml

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    import qlib
    from qlib.utils import init_instance_by_config
    from qlib.workflow import R
    from qlib.workflow.record_temp import (  # pyqlib>=0.9：全部在 record_temp
        PortAnaRecord,
        SigAnaRecord,
        SignalRecord,
    )

    init_cfg = dict(cfg.get("qlib_init") or {})
    if args.provider:
        init_cfg["provider_uri"] = args.provider
    if not init_cfg.get("provider_uri"):
        raise SystemExit("provider_uri 未指定（--provider 或 config.qlib_init.provider_uri）")
    init_cfg.setdefault("region", "cn")
    if args.market:
        cfg["market"] = args.market
        cfg["dataset"]["kwargs"]["handler"]["kwargs"]["instruments"] = args.market
    print(f"[runner] qlib.init(provider_uri={init_cfg['provider_uri']}, region={init_cfg['region']})", flush=True)
    qlib.init(**init_cfg)
    # qlib 默认 uri 是 "file:" + 绝对路径（单斜杠），新版 mlflow 会把它当
    # 相对路径解析出 ./Users/... 游离目录 —— 显式规范为 file:///<abs>/mlruns。
    from pathlib import Path as _Path

    from qlib.config import C as _C

    _C.exp_manager.setdefault("kwargs", {})["uri"] = f"file://{_Path.cwd() / 'mlruns'}"

    dataset = init_instance_by_config(cfg["dataset"])
    model = init_instance_by_config(cfg["model"])

    with R.start(experiment_name=args.exp_name, recorder_name=args.recorder_name):
        R.log_params(
            model=cfg["model"].get("class"), handler=cfg["dataset"]["kwargs"]["handler"]["class"],
            market=cfg.get("market"),
        )
        print("[runner] model.fit ...", flush=True)
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        recorder = R.get_recorder()
        print("[runner] SignalRecord(预测) ...", flush=True)
        SignalRecord(model, dataset, recorder).generate()
        print("[runner] SigAnaRecord(IC/ICIR/RankIC) ...", flush=True)
        SigAnaRecord(recorder).generate()
        if cfg.get("port_analysis"):
            print("[runner] PortAnaRecord(组合回测) ...", flush=True)
            PortAnaRecord(recorder, cfg["port_analysis"], "day").generate()
        metrics = recorder.list_metrics()

    print("[runner] metrics:", flush=True)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
