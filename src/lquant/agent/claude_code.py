"""ClaudeCodeAgentService：以子进程驱动无头 claude CLI，stream-json 映射为事件流。

本模块只负责 claude 特有的两件事：**命令行形状**与**输出解析**。
会话落库、单飞、超时、取消、子进程回收全在 :class:`CliAgentService`（唯一实现）。
"""

from __future__ import annotations

from pathlib import Path

from lquant.agent.claude_json import parse_stream_line
from lquant.agent.cli_agent import CliAgentService
from lquant.agent.sessions import SessionStore
from lquant.agent.spawn import SpawnedChild

#: 兼容旧引用：`_Child` 已抽到 agent/spawn.py，语义不变（测试会 monkeypatch 它）
_Child = SpawnedChild

_SYSTEM_PROMPT = (
    "你是 lquant 量化研究平台的 AI 助手。回答 A 股相关问题时："
    "数据访问优先级见工作区 CLAUDE.md（MCP 工具 → .claude/skills 里的 skill 或本机 HTTP API "
    "→ lquant CLI → 临时脚本）；先查数、结论先行，标注数据来源与时点，"
    "读不到数据就如实说「暂无数据」不编造数字，用中文回答。"
)


class ClaudeCodeAgentService(CliAgentService):
    """每次 send_message 起一个 ``claude -p`` 子进程，cwd 为工作区。

    续聊依赖 claude CLI 的 ``--resume``：首轮 result 事件的 session_id 落库，
    后续轮次带上继续同一 CLI 会话。
    """

    label = "claude_code"
    stderr_tag = "claude"

    def __init__(
        self,
        store: SessionStore,
        claude_path: str | None = None,
        claude_args: list[str] | None = None,
        workspace_dir: str | None = None,
        root: Path | str | None = None,
        timeout_seconds: int | None = None,
        skip_permissions: bool | None = None,
    ) -> None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        super().__init__(store)
        self._claude_path = claude_path or s.agent.claude_path
        self._claude_args = claude_args or []
        # 无头 claude 需要跳过交互式授权，否则会挂住；但它是「全自主」权限。
        # 做成开关（默认保持原行为），不要散在命令行里硬编码。
        self._skip_permissions = (
            s.agent.skip_permissions if skip_permissions is None else skip_permissions)
        self._init_runtime(
            workspace_dir=workspace_dir or s.agent.workspace_dir,
            root=s.root if root is None else root,
            timeout_seconds=timeout_seconds if timeout_seconds is not None
            else s.agent.timeout_seconds,
        )

    def _build_cmd(self, content: str, cli_sid: str | None) -> list[str]:
        cmd = [
            self._claude_path,
            "-p", content,
            "--output-format", "stream-json",
            "--verbose",
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--mcp-config", str(self._workspace / ".claude" / "mcp.json"),
        ]
        if self._skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if cli_sid:
            cmd += ["--resume", cli_sid]
        cmd += self._claude_args
        return cmd

    def _parse_line(self, raw: bytes) -> list[dict]:
        return parse_stream_line(raw.decode(errors="replace"))
