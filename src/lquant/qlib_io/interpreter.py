"""qlib 解释器探测（CLI 与 server 共用）。"""
from __future__ import annotations

import os
from pathlib import Path


def find_qlib_python(python: str | None = None) -> str | None:
    """定位可用 qlib 解释器。None=当前解释器可 import qlib；""=找不到。"""
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
