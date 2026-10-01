"""agent 运行时工作区脚手架。

为 CLI provider 在磁盘上准备一次性的工作区：

- ``CLAUDE.md`` + ``AGENTS.md`` —— 同一份角色与数据访问优先级说明。两个文件
  内容一致、各写一份，是因为 **claude 读 CLAUDE.md、codex 读 AGENTS.md**；
  只写一个的话另一个 provider 起来就是个「没有口径的裸 agent」。
- ``.claude/mcp.json`` —— lquant MCP server（claude 用 ``--mcp-config`` 指过来；
  codex 把同一份规格翻成 ``-c mcp_servers.lquant.*``，见 ``mcp_server_spec``）。
- ``.claude/skills/`` —— 从 ``config/skills/`` 同步，并把 ``${LQ_API_BASE}``
  换成实际基址（codex 没有原生 skill 机制，但能读这里的文件，故 AGENTS.md
  直接指向同一目录，不另存一份）。

为什么指引只能有一处：CLAUDE.md/AGENTS.md、``--append-system-prompt``、skill 文件
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


#: 角色说明要落到两个文件名：claude 认 CLAUDE.md，codex 认 AGENTS.md
_ROLE_DOC_NAMES = ("CLAUDE.md", "AGENTS.md")


def _write_role_docs(workspace: Path, root: Path) -> None:
    """两个文件名写同一份内容（内容只有一处模板，避免两个 provider 口径分叉）。"""
    text = _CLAUDE_MD_TEMPLATE.format(root=root, api_base=api_base_url())
    for name in _ROLE_DOC_NAMES:
        (workspace / name).write_text(text, encoding="utf-8")


def mcp_server_spec(root: Path, python: str | None = None,
                    enabled_tools: set[str] | None = None) -> dict:
    """lquant MCP server 的启动规格 —— **两个 provider 共用这一份**。

    claude 把它写成 ``.claude/mcp.json``，codex 把它翻成
    ``-c mcp_servers.lquant.*``（见 agent/codex.py）。放在这里是为了避免
    「两套 MCP 配置各自演化」：host 解释器、模块入口、环境变量只有一处定义。

    ``enabled_tools`` 为会话级工具白名单，经 ``LQ_MCP_ENABLED_TOOLS`` 注入给
    server 进程。注意 **空集要写成存在但为空串** —— server 侧把「变量缺失」
    读作全开、「存在」读作按名单裁剪，两者语义不同，不能合并成一个空值。
    ``None`` 表示不注入该变量（全开）。
    """
    env = {
        "LQ_ROOT": str(root),
        "PYTHONPATH": str(root / "src"),
    }
    if enabled_tools is not None:
        env["LQ_MCP_ENABLED_TOOLS"] = ",".join(sorted(enabled_tools))
    return {
        "type": "stdio",
        "command": python or sys.executable,
        "args": list(_MCP_SERVER_ARGS),
        "env": env,
    }


def _write_mcp_json(workspace: Path, root: Path,
                    enabled_tools: set[str] | None = None) -> None:
    mcp_config = {"mcpServers": {"lquant": mcp_server_spec(root, enabled_tools=enabled_tools)}}
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(
        json.dumps(mcp_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _sync_skills(workspace: Path, root: Path,
                 enabled: set[str] | None = None) -> None:
    """每次调用先删后拷，保证 skills 与 config/skills/ 一致。

    拷贝后把 ``${LQ_API_BASE}`` 替换成实际基址：skill 是跟着仓库分发的静态文件，
    不能把部署相关的主机端口写死在里面（换端口即失效）。

    ``enabled`` 是会话级启用集：``None`` = 全量（老调用方行为不变），
    否则只拷启用的。**按源目录实际子目录取交集**，不直接拼路径 —— 名单来自
    请求体/数据库，拼路径会让 ``../`` 把任意目录拷进工作区。
    """
    skills_src = root / "config" / "skills"
    if not skills_src.is_dir():
        return
    skills_dst = workspace / ".claude" / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    skills_dst.mkdir(parents=True, exist_ok=True)
    for src in sorted(skills_src.iterdir()):
        if not src.is_dir():
            continue
        if enabled is not None and src.name not in enabled:
            continue
        shutil.copytree(src, skills_dst / src.name)
    base = api_base_url()
    for md in skills_dst.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        if _API_BASE_PLACEHOLDER in text:
            md.write_text(text.replace(_API_BASE_PLACEHOLDER, base), encoding="utf-8")


def ensure_workspace(workspace_dir: str, root: Path,
                     enabled_skills: set[str] | None = None,
                     enabled_tools: set[str] | None = None) -> Path:
    """幂等创建 agent 工作区并生成脚手架文件，返回工作区路径。

    ``enabled_skills`` / ``enabled_tools`` 是会话级能力裁剪（None = 不裁剪）。
    """
    root = Path(root)
    workspace = root / workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    _write_role_docs(workspace, root)
    _write_mcp_json(workspace, root, enabled_tools=enabled_tools)
    _sync_skills(workspace, root, enabled=enabled_skills)
    return workspace
