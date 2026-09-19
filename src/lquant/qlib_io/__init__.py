"""qlib 接入层：lquant 日线湖 → qlib 二进制数据 → qlib 工作流。

- `export.py`：把 parquet 日线湖导出为 qlib 的 calendars/instruments/features
  二进制格式（不依赖 pyqlib，纯 polars/pyarrow，在主 venv 运行）；
- `runner.py`：独立脚本，在 **qlib 专用 venv**（pyqlib 已安装）内运行
  qrun 风格工作流（训练 + IC 分析 + 组合回测），不 import lquant。

qlib 运行时依赖树较重，本仓库刻意不把它装进主 venv（与 btval venv 同一
隔离思路）。安装方式：

    python -m venv .venv-qlib
    .venv-qlib/bin/pip install pyqlib lightgbm

之后 `lq qlib workflow` 会自动探测：主 venv 有 pyqlib 就进程内跑，
否则用 `--python`（或 LQ_QLIB_PYTHON 环境变量）指向的 venv 解释器起子进程。
"""
