"""配置加载：YAML + 环境变量插值 + 单例缓存。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

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


def api_base_url() -> str:
    """本机 API 基址（与 lquant.sh 的 LQ_API_HOST / LQ_API_PORT 同一组开关）。

    Agent Card 的自述地址、工作区 CLAUDE.md 里给 agent 的 HTTP base 都用它 ——
    只有一处定义，改端口不会出现「文档说 8000、实际跑在 8001」。
    """
    host = os.getenv("LQ_API_HOST", "127.0.0.1")
    port = os.getenv("LQ_API_PORT", "8000")
    return f"http://{host}:{port}"


BACKTEST_WORKERS_MAX = 4


def clamp_backtest_workers(n: int) -> int:
    return max(0, min(BACKTEST_WORKERS_MAX, int(n)))


class AgentConfig(BaseModel):
    # 默认就是内置 Claude Code：「问 AI」= 问 Claude Code。
    # codex 复用同一套无头 CLI 骨架（agent/codex.py）；mock 只是脚本化演示
    # （不调 LLM），需要无 CLI 环境跑通链路时显式选它。
    provider: str = "claude_code"
    claude_path: str = "claude"
    codex_path: str = "codex"
    workspace_dir: str = "data/agent_workspace"
    timeout_seconds: int = 300
    # 无头 CLI 需要跳过交互式授权，否则会挂在确认提示上；
    # 但那是「全自主」权限，等价于让 CLI 任意读写本机。
    # 默认保持 True 以免破坏既有用法，service.py 启用时会打印告警。
    # 只要不需要 CLI 落盘/执行命令，就设成 false。
    #
    # codex 侧另有一层：实测审批策略为 never 时 **MCP 工具调用会被直接拒绝**
    # （MCP tool call requires approval），所以 True 时用
    # --dangerously-bypass-approvals-and-sandbox，false 时退到 -s workspace-write。
    skip_permissions: bool = True
    # 模型不在这里配：两个 CLI 都复用自身的模型配置
    # （claude 的 settings / ANTHROPIC_*；codex 的 ~/.codex/config.toml），
    # 子进程按原样继承环境，见 docs/AGENT_MODEL.md。

    # 新建会话的**全局默认能力集**，只用于「问 AI」页新建会话弹层的**预填**
    # （见 server/api/agent.py 的 /capabilities 与前端 resolveDefaults）。
    #
    # ⚠️ 它**不是**运行时兜底：会话没有 agent_config（老会话 / A2A 建的 /
    # 前端降级直接建）时，能力集是 `{}` = **不裁剪（全开）**，不会回退到这里。
    # 所以把 default_mcp_tools 设成 none 只影响新建会话的默认勾选，管不住
    # 已经存在或由 A2A 建的会话 —— 要收紧那类会话得另想办法。
    #
    # None = 不裁剪（全开）；[] = 一个都不启用。两者语义不同，不要合并。
    default_skills: list[str] | None = None
    default_mcp_tools: list[str] | None = None

    @field_validator("default_skills", "default_mcp_tools", mode="before")
    @classmethod
    def _parse_capability_list(cls, v: Any) -> Any:
        """接受 YAML 列表，或环境变量插值出的逗号串。

        字符串形态：``all`` / 空白 = 不裁剪（``None``）；``none`` = 一个都不启用
        （``[]``）；其余按逗号切分。

        为什么要有显式的 ``none``：这两个开关的名字与工作区里那个
        ``LQ_MCP_ENABLED_TOOLS`` 很像，但后者的空串表示**一个都不开**。
        没有 ``none`` 的话，「想配成不启用」只能写成空串，而空串在这里是
        「全开」—— 同一形状两种含义，是最容易静默给多权限的坑。
        """
        if isinstance(v, str):
            v = v.strip()
            if not v or v == "all":
                return None
            if v == "none":
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


class Settings(BaseModel):
    root: Path = Field(default_factory=find_root)
    env: str = "dev"
    agent: AgentConfig = Field(default_factory=AgentConfig)
    timezone: str = "Asia/Shanghai"
    duckdb_path: str = "./data/duckdb/lquant.duckdb"
    parquet_dir: str = "./data/parquet"
    cache_dir: str = "./data/cache"
    ingest_concurrency: int = 4
    ingest_watchdog_sec: int = 120
    redis_url: str = "redis://localhost:6379/0"
    monitor_enabled: bool = True
    monitor_flush_interval_sec: int = 10
    monitor_sample_interval_sec: int = 5
    monitor_retention_days: int = 7
    monitor_db_path: str = ""
    backtest_workers: int = 2
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
    agent = raw.get("agent", {})
    return Settings(
        root=root,
        env=raw.get("env", "dev"),
        agent=AgentConfig(**agent),
        timezone=raw.get("timezone", "Asia/Shanghai"),
        duckdb_path=str(paths.get("duckdb", "./data/duckdb/lquant.duckdb")),
        parquet_dir=str(paths.get("parquet", "./data/parquet")),
        cache_dir=str(paths.get("cache", "./data/cache")),
        ingest_concurrency=int(ingest.get("concurrency", 4)),
        ingest_watchdog_sec=int(ingest.get("watchdog_sec", 120)),
        redis_url=os.getenv("LQ_REDIS_URL", "redis://localhost:6379/0"),
        monitor_enabled=(
            os.getenv("LQ_MONITOR_ENABLED") != "0"
            if os.getenv("LQ_MONITOR_ENABLED") is not None
            else bool((raw.get("monitor", {}) or {}).get("enabled", True))),
        monitor_flush_interval_sec=int(
            (raw.get("monitor", {}) or {}).get("flush_interval_sec", 10)),
        monitor_sample_interval_sec=int(
            (raw.get("monitor", {}) or {}).get("sample_interval_sec", 5)),
        monitor_retention_days=int(
            (raw.get("monitor", {}) or {}).get("retention_days", 7)),
        monitor_db_path=os.getenv("LQ_MONITOR_DB", "") or str(
            Path(str(paths.get("duckdb", "./data/duckdb/lquant.duckdb")))
            .with_name("lquant.monitor.duckdb").resolve()),
        backtest_workers=int(os.getenv(
            "LQ_BACKTEST_WORKERS",
            str((raw.get("monitor", {}) or {}).get("backtest_workers", 2)))),
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
