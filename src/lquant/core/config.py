"""配置加载：YAML + 环境变量插值 + 单例缓存。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _interpolate(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    if isinstance(node, str):
        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            return os.getenv(name, default if default is not None else "")
        return _ENV_RE.sub(sub, node)
    return node


def find_root() -> Path:
    env = os.getenv("LQ_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists():
            return p
    return Path.cwd()


class Settings(BaseModel):
    root: Path = Field(default_factory=find_root)
    env: str = "dev"
    timezone: str = "Asia/Shanghai"
    duckdb_path: str = "./data/duckdb/lquant.duckdb"
    parquet_dir: str = "./data/parquet"
    cache_dir: str = "./data/cache"
    ingest_concurrency: int = 4
    ingest_watchdog_sec: int = 120
    redis_url: str = "redis://localhost:6379/0"
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = find_root()
    raw: dict[str, Any] = {}
    app = root / "config" / "app.yaml"
    if app.exists():
        raw = _interpolate(yaml.safe_load(app.read_text(encoding="utf-8")) or {})

    paths = raw.get("paths", {})
    ingest = raw.get("ingest", {})
    return Settings(
        root=root,
        env=raw.get("env", "dev"),
        timezone=raw.get("timezone", "Asia/Shanghai"),
        duckdb_path=str(paths.get("duckdb", "./data/duckdb/lquant.duckdb")),
        parquet_dir=str(paths.get("parquet", "./data/parquet")),
        cache_dir=str(paths.get("cache", "./data/cache")),
        ingest_concurrency=int(ingest.get("concurrency", 4)),
        ingest_watchdog_sec=int(ingest.get("watchdog_sec", 120)),
        redis_url=os.getenv("LQ_REDIS_URL", "redis://localhost:6379/0"),
        raw=raw,
    )


@lru_cache(maxsize=8)
def load_yaml(rel: str) -> dict[str, Any]:
    """加载 config/ 下的 yaml（相对路径，如 `rules/cn_a_share.yaml`）。"""
    p = get_settings().config_dir / rel
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    return _interpolate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def load_providers() -> dict[str, Any]:
    return load_yaml("providers.yaml")
