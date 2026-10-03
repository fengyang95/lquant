"""会话级能力配置：provider / skill / MCP 工具的校验、清单与 skill 文件读写。

「能力」有三层，建会话时都能选定：

- ``provider``：谁来答（claude_code / codex / mock），**建后锁定**
- ``skills``：工作区 ``.claude/skills/`` 里放哪几个（``None`` = 全放），建后可改
- ``mcp_tools``：MCP server 暴露哪几个工具（``None`` = 全放），建后可改

为什么只有 provider 锁死：CLI 侧会话 id（claude 的 ``session_id`` / codex 的
``thread_id``）在库里共用一列，中途换 provider 会「续接」到另一个 CLI 的会话，
上下文直接串了。skills / mcp_tools 不参与会话寻址，且每轮回答都按当前配置
**重建工作区**（见 ``CliAgentService._workspace_for``），所以允许会话内修改、
下一轮生效（:func:`normalize_capability_update` + ``SessionStore.set_agent_config``）。

校验刻意分两档：

- provider 与 mcp_tools 有**静态注册表**（``PROVIDERS`` / ``TOOL_HANDLERS``），
  名单里出现不存在的名字直接拒绝 —— 拼错一个工具名只会静默少给一项能力，
  用户看到的是「它怎么不会用这个工具」，必须挡在建会话那一步。
- skill 是**文件系统**里的目录，只校验名字形状（防目录穿越），不校验存在性：
  skill 目录后来被删掉的老会话仍应能继续跑，那一路由工作区同步与真实目录
  取交集兜底（见 ``workspace._sync_skills``）。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from lquant.agent.a2a.card import parse_frontmatter

#: 可选的 provider（顺序即前端展示顺序）
PROVIDERS: tuple[str, ...] = ("claude_code", "codex", "mock")

PROVIDER_LABELS: dict[str, str] = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "mock": "Mock（脚本化演示，非 LLM）",
}

#: skill 目录名 / frontmatter name 的合法形状。**同时是路径安全边界**：
#: 只允许小写字母数字与短横线，``../`` 这类穿越名根本构造不出来。
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SKILL_FILENAME = "SKILL.md"


class CapabilityError(ValueError):
    """能力配置非法（API 层转 400）。"""


def skills_dir(root: Path) -> Path:
    return Path(root) / "config" / "skills"


def _clean_names(value: object, field: str) -> list[str] | None:
    """``None`` 原样返回（= 不裁剪）；否则必须是字符串列表。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise CapabilityError(f"{field} 必须是字符串列表")
    out: list[str] = []
    for x in value:
        if not isinstance(x, str) or not x.strip():
            raise CapabilityError(f"{field} 里必须是非空字符串")
        out.append(x.strip())
    return out


def known_mcp_tools() -> set[str]:
    """已注册的 MCP 工具名（会话配置与运行时默认值共用同一份注册表）。"""
    from lquant.agent.mcp_server import TOOL_HANDLERS  # noqa: PLC0415

    return set(TOOL_HANDLERS)


#: 兼容旧引用（模块内多处已按私有名调用过）
_known_mcp_tools = known_mcp_tools


def check_skill_names(names: list[str]) -> None:
    """skill 名单的**形状**校验（非法 → :class:`CapabilityError`）。

    只校验形状、不校验存在性：老的会话配置里可以有「目录后来被删掉」的 skill，
    它仍应能继续跑（存在性由工作区同步与真实目录取交集兜底）。
    """
    bad = [n for n in names if not SKILL_NAME_RE.match(n)]
    if bad:
        raise CapabilityError(f"非法 skill 名: {', '.join(bad)}")


def check_default_skill_names(names: list[str]) -> None:
    """**默认** skill 名单：形状 + 存在性。

    比 :func:`check_skill_names` 多查一次存在性，因为默认名单是「以后每次新建
    会话都照这个来」：写错一个名字，新建的会话会静默少一项能力，用户只会觉得
    「它怎么不会用这个 skill」，而这种缺失在界面上看不出来。会话级配置不查
    （见上），global 默认查 —— 差别就是「改错了能不能当场发现」。
    """
    check_skill_names(names)
    from lquant.core.config import get_settings  # noqa: PLC0415

    known = {s["name"] for s in list_skills(get_settings().root)}
    missing = sorted(set(names) - known)
    if missing:
        raise CapabilityError(
            f"skill 不存在: {', '.join(missing)}（config/skills/ 下没有这个目录）")


#: 建会话后仍可修改的**能力**项（provider 不在其中：换 provider 会续到别人的
#: CLI 会话，见 ``SessionStore.set_agent_config`` 与 ``server.api.ask`` 的说明）。
MUTABLE_CAPABILITY_FIELDS: tuple[str, ...] = ("skills", "mcp_tools")

#: 建会话后仍可修改的**运行参数**：单次回答超时、CLI 全自主权限。
#:
#: 为什么这两项要能按会话改：它们是「这一轮敢不敢放手让它跑」的旋钮，而不同
#: 会话的诉求天然不同 —— 一个只是问答的会话不该给全自主权限，一个跑长任务的
#: 会话又需要更长的超时。全局值（AI 设置面板里的 ``agent.*``）是**默认档**，
#: 这里存的是本会话的覆盖；值 ``None`` = 不覆盖、跟随全局（与 ``skills`` 的
#: ``None`` 不是同一回事，别类推：那边的 ``None`` 是「不裁剪」这个**实义值**，
#: 这边的 ``None`` 是「回到默认」）。
MUTABLE_RUN_FIELDS: tuple[str, ...] = ("timeout_seconds", "skip_permissions")

#: ``PATCH /sessions/{sid}/config`` 能收的全部字段。
MUTABLE_SESSION_FIELDS: tuple[str, ...] = (
    *MUTABLE_CAPABILITY_FIELDS, *MUTABLE_RUN_FIELDS)

#: 单次回答超时的合法区间（秒）。**唯一一份**：写入侧（SettingsStore）、
#: 运行时解析（agent.runtime）、会话级校验（这里）都引用它，避免三处各写
#: 一个数字后悄悄分叉 —— 那种分叉的表现是「设置页存不进去但接口收下了」。
TIMEOUT_MIN_SECONDS = 10
TIMEOUT_MAX_SECONDS = 3600


def clean_timeout_seconds(value: object) -> int | None:
    """会话级 / 全局超时值的校验与归一。``None`` = 回到默认档。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CapabilityError(f"超时（timeout_seconds）必须是整数秒，收到 {value!r}")
    try:
        val = int(float(value))
    except (TypeError, ValueError):
        raise CapabilityError(
            f"超时（timeout_seconds）必须是整数秒，收到 {value!r}") from None
    if not TIMEOUT_MIN_SECONDS <= val <= TIMEOUT_MAX_SECONDS:
        raise CapabilityError(
            f"超时应在 {TIMEOUT_MIN_SECONDS}~{TIMEOUT_MAX_SECONDS} 秒之间，收到 {val}")
    return val


def clean_skip_permissions(value: object) -> bool | None:
    """会话级权限开关的校验与归一。``None`` = 回到默认档。"""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CapabilityError(f"skip_permissions 必须是 true/false，收到 {value!r}")
    return value


def normalize_agent_config(body: dict) -> dict:
    """把建会话请求体里的能力字段规整成落库用的 dict。

    没出现的字段不进结果（保持 ``{}`` 语义 = 未指定 → 走全局默认）。
    """
    cfg: dict = {}
    if body.get("provider") is not None:
        provider = body["provider"]
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise CapabilityError(
                f"未知 provider: {provider}（可选：{'/'.join(PROVIDERS)}）")
        cfg["provider"] = provider
    if "skills" in body:
        names = _clean_names(body["skills"], "skills")
        if names is not None:
            check_skill_names(names)
        cfg["skills"] = names
    if "mcp_tools" in body:
        names = _clean_names(body["mcp_tools"], "mcp_tools")
        if names is not None:
            unknown = sorted(set(names) - known_mcp_tools())
            if unknown:
                raise CapabilityError(f"未知 MCP 工具: {', '.join(unknown)}")
        cfg["mcp_tools"] = names
    return cfg


def normalize_capability_update(body: dict) -> dict:
    """把「会话内改配置」的请求体规整成落库用的 dict。

    准入字段见 :data:`MUTABLE_SESSION_FIELDS`：``skills`` / ``mcp_tools``
    （能力）与 ``timeout_seconds`` / ``skip_permissions``（运行参数）。

    与 :func:`normalize_agent_config` 的区别只在**准入字段**：建会话时按空体
    （``{}``）表示「全走全局默认」，这里空体是错误 —— 一次什么都没改的 PATCH
    说明前端状态坏了，静默 200 会让人以为改生效了。能力值的校验则**完全复用**
    ``normalize_agent_config``（skill 名形状、MCP 工具必须在注册表里），
    不写第二份，避免两处口径分叉。

    返回值只含出现在 ``body`` 里的键；``None``（能力侧 = 全开；运行参数侧
    = 回到全局默认）与 ``[]``（= 全不启用）原样保留、不合并 —— 语义与建会话一致。
    """
    if "provider" in body:
        # provider 锁定：CLI 侧会话 id（claude 的 session_id / codex 的
        # thread_id）共用一列，中途换 provider 续接的是另一个 CLI 的会话。
        # 「换个后端接着问」是**另开会话**（见 server.api.ask 的 fork 接口），
        # 不是改这一条配置。
        raise CapabilityError("provider 建会话时锁定，不可修改（换后端请另开会话）")
    unknown = [k for k in body if k not in MUTABLE_SESSION_FIELDS]
    if unknown:
        raise CapabilityError(
            f"不支持的配置项: {', '.join(str(k) for k in unknown)}"
            f"（只支持 {' / '.join(MUTABLE_SESSION_FIELDS)}）")
    if not any(k in body for k in MUTABLE_SESSION_FIELDS):
        raise CapabilityError(
            f"没有可修改的配置项（只支持 {' / '.join(MUTABLE_SESSION_FIELDS)}）")
    out = normalize_agent_config(
        {k: v for k, v in body.items() if k in MUTABLE_CAPABILITY_FIELDS})
    if "timeout_seconds" in body:
        out["timeout_seconds"] = clean_timeout_seconds(body["timeout_seconds"])
    if "skip_permissions" in body:
        out["skip_permissions"] = clean_skip_permissions(body["skip_permissions"])
    return out


# ---- 可用能力清单（/api/agent/capabilities） --------------------------------


def available_providers() -> list[dict]:
    """provider 清单 + 本机可用性（CLI 是否在 PATH 上）。

    可用性只是**提示**，不是门禁：CLI 不在 PATH 时仍可选，报错会走正常失败路径
    （``无法启动 xxx CLI``），比在这里静默把它从选项里抹掉更容易排查。
    """
    from lquant.core.config import get_settings  # noqa: PLC0415

    agent = get_settings().agent
    paths = {"claude_code": agent.claude_path, "codex": agent.codex_path, "mock": ""}
    out: list[dict] = []
    for p in PROVIDERS:
        exe = paths.get(p, "")
        out.append({
            "id": p,
            "label": PROVIDER_LABELS.get(p, p),
            "available": p == "mock" or bool(shutil.which(exe)),
        })
    return out


def list_mcp_tools() -> list[dict]:
    from lquant.agent.mcp_server import TOOLS_SPEC  # noqa: PLC0415

    return [{"name": t["name"], "description": str(t.get("description", ""))}
            for t in TOOLS_SPEC]


def _skill_meta(md: Path) -> dict | None:
    if not md.is_file():
        return None
    return parse_frontmatter(md.read_text(encoding="utf-8"))


def list_skills(root: Path) -> list[dict]:
    """``config/skills/`` 下的 skill 清单。

    与 Agent Card 的 ``load_skills`` 不同，**坏 skill 也列出来**并标
    ``valid=False``：那是给人看的编辑清单，把解析不了的那条藏起来，用户就
    永远修不好它（Agent Card 是给外部发现用的，跳过是对的，两者口径不同）。
    """
    d = skills_dir(root)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(d.iterdir()):
        if not child.is_dir():
            continue
        meta = _skill_meta(child / SKILL_FILENAME)
        tags = (meta or {}).get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        name = str((meta or {}).get("name") or "").strip()
        desc = str((meta or {}).get("description") or "").strip()
        out.append({
            "name": child.name,
            "description": desc,
            "tags": [str(t) for t in tags],
            # 目录名也要过形状校验：`normalize_agent_config` 会拒收非法名，
            # 清单若把它当合法项列出来，前端全选后建会话必然 400，且用户
            # 找不到哪里不对（他在界面上看到的是个正常条目）。
            "valid": bool(name and desc and SKILL_NAME_RE.match(child.name)),
        })
    return out


# ---- skill 文件读写（设置页的编辑器） ---------------------------------------


def _skill_path(root: Path, name: str) -> Path:
    """**写入**用：名字必须过形状校验（只允许小写字母数字与短横线）。"""
    if not SKILL_NAME_RE.match(name):
        raise CapabilityError(f"非法 skill 名: {name}（只允许小写字母数字与短横线）")
    return skills_dir(root) / name / SKILL_FILENAME


def _existing_skill_dir(root: Path, name: str) -> Path:
    """**读取 / 删除**用：允许任意目录名，但必须是 ``skills/`` 的**直接子目录**。

    读删为什么放宽到不做形状校验：目录名不合规的 skill 会被 :func:`list_skills`
    列出来（``valid=False``），若读/删也按写入那套形状挡掉，用户在设置页就只能
    盯着一个删不掉的条目 —— 「列出来却什么都干不了」比不列更糟。放宽的同时靠
    parent 校验守住穿越：``../secret`` 解析后父目录不是 ``skills/``，直接拒。
    """
    d = skills_dir(root).resolve()
    p = (d / name).resolve()
    if p.parent != d or not p.is_dir():
        raise FileNotFoundError(f"skill 不存在: {name}")
    return p


def read_skill(root: Path, name: str) -> str:
    p = _existing_skill_dir(root, name) / SKILL_FILENAME
    if not p.is_file():
        raise FileNotFoundError(f"skill 不存在: {name}")
    return p.read_text(encoding="utf-8")


def validate_skill_content(content: str) -> None:
    """SKILL.md 必须有 frontmatter，且含非空 name / description。

    这两项是 Agent Card 与能力清单的**唯一**来源，缺了它这个 skill 对外就是
    不可见的 —— 允许存下来等于允许用户静默地做出一个「装了但没用」的 skill。
    """
    meta = parse_frontmatter(content)
    if meta is None:
        raise CapabilityError("缺少 YAML frontmatter（文件需以 `---` 开头并有结束的 `---`）")
    if not str(meta.get("name") or "").strip():
        raise CapabilityError("frontmatter 缺少 name")
    if not str(meta.get("description") or "").strip():
        raise CapabilityError("frontmatter 缺少 description")


def write_skill(root: Path, name: str, content: str) -> None:
    p = _skill_path(root, name)  # 先过名字形状（路径安全），再校验内容
    validate_skill_content(content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def delete_skill(root: Path, name: str) -> None:
    shutil.rmtree(_existing_skill_dir(root, name))
