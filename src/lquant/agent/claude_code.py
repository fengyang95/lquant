"""ClaudeCodeAgentService：以子进程驱动无头 claude CLI，stream-json 映射为事件流。

本模块只负责 claude 特有的两件事：**命令行形状**与**输出解析**。
会话落库、单飞、超时、取消、子进程回收全在 :class:`CliAgentService`（唯一实现）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from lquant.agent.claude_json import StreamParser, parse_stream_line
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
    provider = "claude_code"

    def __init__(
        self,
        store: SessionStore,
        claude_path: str | None = None,
        claude_args: list[str] | None = None,
        workspace_dir: str | None = None,
        root: Path | str | None = None,
        timeout_seconds: int | None = None,
        skip_permissions: bool | None = None,
        partial_messages: bool | None = None,
    ) -> None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        super().__init__(store)
        self._claude_path = claude_path or s.agent.claude_path
        self._claude_args = claude_args or []
        # 权限 / token 级流式都**不在构造时定值**（None = 跟随运行时配置）：
        # service 实例按 (store, provider) 缓存，快照下来前端改完就不生效了。
        # 详见 CliAgentService._resolve_skip_permissions / _resolve_partial_messages。
        self._init_runtime(
            workspace_dir=workspace_dir or s.agent.workspace_dir,
            root=s.root if root is None else root,
            timeout_seconds=timeout_seconds,
            skip_permissions=skip_permissions,
            partial_messages=partial_messages,
        )

    def _build_cmd(self, content: str, cli_sid: str | None, workspace: Path,
                   cfg: dict | None = None) -> list[str]:
        cmd = [
            self._claude_path,
            "-p", content,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self._resolve_partial_messages(cfg):
            # 与 stream-json 配套：产出 stream_event 增量行（正文逐 token）
            cmd.append("--include-partial-messages")
        cmd += [
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--mcp-config", str(workspace / ".claude" / "mcp.json"),
        ]
        if self._resolve_skip_permissions(cfg):
            cmd.append("--dangerously-skip-permissions")
        if cli_sid:
            cmd += ["--resume", cli_sid]
        cmd += self._claude_args
        return cmd

    def _parse_line(self, raw: bytes) -> list[dict]:
        return parse_stream_line(raw.decode(errors="replace"))

    def _make_line_parser(self) -> Callable[[bytes], list[dict]]:
        """本轮复用**同一个** :class:`StreamParser`：去重需要跨行记住 message.id。

        别退化成每行一个实例 —— 那样拿不到「本消息已发过增量」的状态，
        ``assistant`` 整块会和增量一起落库，正文翻倍。
        """
        parser = StreamParser()
        return lambda raw: parser.feed(raw.decode(errors="replace"))
