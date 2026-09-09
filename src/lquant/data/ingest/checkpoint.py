"""断点续传。

BaoStock 长任务（5000 只标的 / 逐季财务）动辄跑几小时，
中途被杀是常态。每个长任务一个 checkpoint 文件，重跑时自动跳过已完成项。

文件：data/cache/checkpoints/<name>.json
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from lquant.core.config import get_settings


def _dir() -> Path:
    p = Path(get_settings().cache_dir) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Checkpoint:
    """记录"已完成"的键集合，原子写入（写临时文件再 rename）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.path = _dir() / f"{name}.json"
        self._data: dict = {"done": [], "updated_at": None, "meta": {}}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # 坏文件直接当空，不阻塞任务

    @property
    def done(self) -> set[str]:
        return set(self._data.get("done", []))

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark(self, keys: list[str] | set[str]) -> None:
        d = self.done | set(keys)
        self._data["done"] = sorted(d)
        self._flush()

    def unmark(self, retry_keys: list[str] | set[str]) -> None:
        """从 done 集合移除（retry 场景），下次重跑自动重试这些键。"""
        self._data["done"] = sorted(self.done - set(retry_keys))
        self._flush()

    def remaining(self, keys: list[str]) -> list[str]:
        done = self.done
        return [k for k in keys if k not in done]

    def set_meta(self, **kw) -> None:
        self._data.setdefault("meta", {}).update(kw)
        self._flush()

    @property
    def meta(self) -> dict:
        return self._data.get("meta", {})

    def clear(self) -> None:
        self._data = {"done": [], "updated_at": None, "meta": {}}
        self._flush()

    def _flush(self) -> None:
        self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def __len__(self) -> int:
        return len(self.done)
