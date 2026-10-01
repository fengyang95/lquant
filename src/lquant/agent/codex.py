"""CodexAgentService：以子进程驱动无头 codex CLI，JSONL 事件映射为事件流。

与 claude provider 共用 :class:`CliAgentService`（会话/单飞/超时/取消/回收，
一份实现），本模块只负责 codex 特有的命令行形状与输出解析（后者在
:mod:`lquant.agent.codex_json`）。

几个**实机取证**（codex-cli 0.159.3）得来、不看就会踩的坑：

- ``codex exec`` 是唯一非交互入口，``--json`` 才输出 JSONL；
  续聊是子命令形式 ``codex exec <opts> resume <thread_id> <prompt>``
  （选项放 ``resume`` 之前实测可行，且 resume 会回同一个 thread_id）。
- **MCP 调用必须 bypass 审批**：默认（含 ``-s read-only``）下审批策略是
  ``never``，而 MCP 工具被标为需要审批 → 直接报
  ``MCP tool call requires approval, but approval policy is never``，
  工具一个都调不动。故 ``skip_permissions=True`` 时用
  ``--dangerously-bypass-approvals-and-sandbox``（对应 claude 的
  ``--dangerously-skip-permissions``）；关闭时退到 ``-s workspace-write``。
- MCP server 通过 ``-c mcp_servers.<name>.*`` 注入（``-c`` 的 value 按 TOML
  解析），**不写用户的 ``~/.codex/config.toml``** —— 与「平台不落模型配置」
  同一条纪律：只做进程内的命令行覆盖。
- **AGENTS.md**：codex 读 ``AGENTS.md``（不是 ``CLAUDE.md``），
  工作区里两种都写（见 workspace.py）。
- 无 ``--append-system-prompt`` 之类的旗标，角色与口径只能写进 AGENTS.md。
"""

from __future__ import annotations

import sys
from pathlib import Path

from lquant.agent.cli_agent import CliAgentService
from lquant.agent.codex_json import parse_codex_line
from lquant.agent.sessions import SessionStore
from lquant.agent.workspace import mcp_server_spec

#: 工作区 MCP 里的 server 名（与 claude 的 mcp.json 对齐）
_MCP_SERVER_NAME = "lquant"


class CodexAgentService(CliAgentService):
    """每次 send_message 起一个 ``codex exec`` 子进程，cwd 为工作区。"""

    label = "codex"
    stderr_tag = "codex"

    def __init__(
        self,
        store: SessionStore,
        codex_path: str | None = None,
        codex_args: list[str] | None = None,
        workspace_dir: str | None = None,
        root: Path | str | None = None,
        timeout_seconds: int | None = None,
        skip_permissions: bool | None = None,
    ) -> None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        super().__init__(store)
        self._codex_path = codex_path or s.agent.codex_path
        self._codex_args = codex_args or []
        # 同 claude 的 skip_permissions：无头运行必须跳过审批，否则连 MCP
        # 工具都调不动；但它同时关掉沙箱，是「全自主」权限。
        self._skip_permissions = (
            s.agent.skip_permissions if skip_permissions is None else skip_permissions)
        self._init_runtime(
            workspace_dir=workspace_dir or s.agent.workspace_dir,
            root=s.root if root is None else root,
            timeout_seconds=timeout_seconds if timeout_seconds is not None
            else s.agent.timeout_seconds,
        )

    # ---- 命令行 -----------------------------------------------------------

    def _mcp_overrides(self) -> list[str]:
        """把工作区 MCP 规格翻成 ``-c`` 覆盖项（value 按 TOML 解析）。"""
        spec = mcp_server_spec(self._root, python=sys.executable)
        args_toml = "[" + ",".join(f'"{a}"' for a in spec["args"]) + "]"
        env_toml = "{" + ",".join(f'{k}="{v}"' for k, v in spec["env"].items()) + "}"
        return [
            "-c", f'mcp_servers.{_MCP_SERVER_NAME}.command="{spec["command"]}"',
            "-c", f"mcp_servers.{_MCP_SERVER_NAME}.args={args_toml}",
            "-c", f"mcp_servers.{_MCP_SERVER_NAME}.env={env_toml}",
        ]

    def _build_cmd(self, content: str, cli_sid: str | None) -> list[str]:
        cmd = [
            self._codex_path,
            "exec",
            "--json",
            "--color", "never",
            "--skip-git-repo-check",
            "-C", str(self._workspace),
        ]
        if self._skip_permissions:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd += ["-s", "workspace-write"]
        cmd += self._mcp_overrides()
        cmd += self._codex_args
        if cli_sid:
            cmd += ["resume", cli_sid]
        cmd.append(content)
        return cmd

    def _parse_line(self, raw: bytes) -> list[dict]:
        return parse_codex_line(raw.decode(errors="replace"))
