"""agent 运行时工作区脚手架。

为 claude_code provider 在磁盘上准备一次性的 Claude Code 工作区：
CLAUDE.md（角色与数据访问优先级）、.claude/mcp.json（lquant MCP server）、
.claude/skills/（从 config/skills/ 同步）。
"""

import json
import shutil
from pathlib import Path

_MCP_SERVER_ARGS = ["-m", "lquant.agent.mcp_server"]

_CLAUDE_MD_TEMPLATE = """\
# lquant 量化研究助手

你是运行在 lquant 量化研究平台中的 AI 助手，负责回答 A 股相关的量化研究问题：
数据查询、因子分析、回测与策略评估等。

## 数据访问优先级

1. **MCP 工具**：优先使用 `lquant` MCP server 提供的工具。
2. **lquant CLI**：MCP 工具不可用时，使用 `lquant` 命令行。
3. **临时 python 脚本**：以上均不可用时，编写临时 python 脚本直接读取数据。

## 仓库根（只读参考）

仓库根 `{root}` 是源代码与配置的只读参考，不得在其中创建或修改任何文件；
所有产物都写入当前工作区目录。
"""


def _write_claude_md(workspace: Path, root: Path) -> None:
    (workspace / "CLAUDE.md").write_text(
        _CLAUDE_MD_TEMPLATE.format(root=root), encoding="utf-8"
    )


def _write_mcp_json(workspace: Path, root: Path) -> None:
    mcp_config = {
        "mcpServers": {
            "lquant": {
                "type": "stdio",
                "command": "python",
                "args": _MCP_SERVER_ARGS,
                "env": {
                    "LQ_ROOT": str(root),
                    "PYTHONPATH": str(root / "src"),
                },
            }
        }
    }
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(
        json.dumps(mcp_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _sync_skills(workspace: Path, root: Path) -> None:
    """每次调用先删后拷，保证 skills 与 config/skills/ 一致。"""
    skills_src = root / "config" / "skills"
    if not skills_src.is_dir():
        return
    skills_dst = workspace / ".claude" / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    shutil.copytree(skills_src, skills_dst)


def ensure_workspace(workspace_dir: str, root: Path) -> Path:
    """幂等创建 agent 工作区并生成脚手架文件，返回工作区路径。"""
    root = Path(root)
    workspace = root / workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    _write_claude_md(workspace, root)
    _write_mcp_json(workspace, root)
    _sync_skills(workspace, root)
    return workspace
