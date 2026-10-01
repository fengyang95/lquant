"""agent 运行时工作区脚手架。

为 claude_code provider 在磁盘上准备一次性的 Claude Code 工作区：
CLAUDE.md（角色与数据访问优先级）、.claude/mcp.json（lquant MCP server）、
.claude/skills/（从 config/skills/ 同步，并把 ``${LQ_API_BASE}`` 换成实际基址）。

为什么指引只能有一处：CLAUDE.md、``--append-system-prompt``、skill 文件
三者都写给模型看。历史上它们口径互斥（一个只说 MCP、一个只说 HTTP、base URL
还硬编码 localhost:8000），agent 只能自己猜。现在统一为
**MCP → skill/HTTP → CLI → 临时脚本** 的四级优先级，base 由部署环境注入。
"""

import json
import shutil
import sys
from pathlib import Path

from lquant.core.config import api_base_url

_MCP_SERVER_ARGS = ["-m", "lquant.agent.mcp_server"]

#: skill 文件里的占位符，同步时替换成实际 API 基址
_API_BASE_PLACEHOLDER = "${LQ_API_BASE}"

_CLAUDE_MD_TEMPLATE = """\
# lquant 量化研究助手

你是运行在 lquant 量化研究平台中的 AI 助手，负责回答 A 股相关的量化研究问题：
数据查询、因子分析、回测与策略评估等。

## 数据访问优先级

1. **MCP 工具**（首选，免写脚本）：`lquant` MCP server 的 `get_*` 工具，覆盖实时行情、
   日线、内置因子，以及大盘概览 / 涨跌家数 / 板块 / 资金流 / 涨停池 / 龙虎榜 /
   热榜 / 指数 / ETF。
2. **skill**：见 `.claude/skills/`（`a-stock-data` 是 MCP 未覆盖端点的清单，
   `factor-mining` 是因子挖掘与体检的流程）。skill 里的 API base 已指向本机服务。
3. **lquant CLI**：MCP 与 skill 都覆盖不到时，用 `lquant` 命令行。
4. **临时 python 脚本**：最后手段，直接读数据湖。

本机 HTTP API：`{api_base}`。**只允许 GET 只读端点**；
`POST /api/market/collect`、`/api/sync/*` 等会触发数据写入或任务的端点一律不许调。

## 仓库根（只读参考）

仓库根 `{root}` 是源代码与配置的只读参考，不得在其中创建或修改任何文件；
所有产物都写入当前工作区目录。

## 回答口径

- 结论先行，并标注数据来源与时点（实时快照 / 截至某交易日）。
- 读不到数据就如实说「暂无数据」，**不编造数字**。
- 历史价格默认不复权；需要复权口径时明确说明。
"""


def _write_claude_md(workspace: Path, root: Path) -> None:
    (workspace / "CLAUDE.md").write_text(
        _CLAUDE_MD_TEMPLATE.format(root=root, api_base=api_base_url()), encoding="utf-8"
    )


def _write_mcp_json(workspace: Path, root: Path) -> None:
    mcp_config = {
        "mcpServers": {
            "lquant": {
                "type": "stdio",
                "command": sys.executable,
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
    """每次调用先删后拷，保证 skills 与 config/skills/ 一致。

    拷贝后把 ``${LQ_API_BASE}`` 替换成实际基址：skill 是跟着仓库分发的静态文件，
    不能把部署相关的主机端口写死在里面（换端口即失效）。
    """
    skills_src = root / "config" / "skills"
    if not skills_src.is_dir():
        return
    skills_dst = workspace / ".claude" / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    shutil.copytree(skills_src, skills_dst)
    base = api_base_url()
    for md in skills_dst.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        if _API_BASE_PLACEHOLDER in text:
            md.write_text(text.replace(_API_BASE_PLACEHOLDER, base), encoding="utf-8")


def ensure_workspace(workspace_dir: str, root: Path) -> Path:
    """幂等创建 agent 工作区并生成脚手架文件，返回工作区路径。"""
    root = Path(root)
    workspace = root / workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    _write_claude_md(workspace, root)
    _write_mcp_json(workspace, root)
    _sync_skills(workspace, root)
    return workspace
