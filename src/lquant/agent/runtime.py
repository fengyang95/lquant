"""问 AI 的运行时配置：代码 / ``config/app.yaml`` 默认 < 前端写入 ``app_setting`` 的覆盖。

**为什么必须有这一层。** 设置页把值写进 ``app_setting`` 表，但 agent 侧一直读
``get_settings().agent``（``lru_cache``，只认 ``config/app.yaml``）—— 于是
``agent.provider`` 这类配置在界面上显示「已修改」，实际一点没变（改完不重启不生效，
重启了也不生效，因为覆盖值压根没被读过）。运行时值统一在这里合并，agent 模块
一律从 :func:`effective_agent_config` 取，**不要再直接读 ``get_settings().agent``**。

覆盖层的读法刻意与 :class:`~lquant.core.settings_store.SettingsStore` 的 ``all()``
分开：那个每次调用都要 ``_ensure_table()``（拿**写**连接跑 DDL），放在每条消息的
关键路径上不合适。这里只做一次只读查询，表缺失/库锁/未建库都只意味着「没有覆盖」，
绝不阻断主链路 —— 配置读不到就用代码默认，是这里唯一说得清的降级。
"""
from __future__ import annotations

from typing import Any

from lquant.core.config import get_settings, parse_capability_list

#: 可被运行时覆盖的 agent 配置：``AgentConfig`` 字段名 → ``app_setting`` 的 key
RUNTIME_KEYS: dict[str, str] = {
    "provider": "agent.provider",
    "default_skills": "agent.default_skills",
    "default_mcp_tools": "agent.default_mcp_tools",
    "timeout_seconds": "agent.timeout_seconds",
    "skip_permissions": "agent.skip_permissions",
    "partial_messages": "agent.partial_messages",
    "max_concurrent_runs": "agent.max_concurrent_runs",
}

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")

#: 「解析不出来」的哨兵。**必须与 None 分开**：能力名单的 ``None`` 是合法值
#: （= 不裁剪 / 全开），拿 None 当「没覆盖」会把「用户刚把默认能力集设成全开」
#: 静默还原成 yaml 默认值 —— 同一形状两种含义，正是本仓反复踩的坑。
_INVALID: object = object()


def _raw_overrides() -> dict[str, str]:
    """``app_setting`` 里 agent.* 的覆盖值（读不到 → 空，视为没有覆盖）。"""
    try:
        from lquant.core.db import reader  # noqa: PLC0415

        with reader() as con:
            rows = con.execute(
                "SELECT setting_key, setting_value FROM app_setting "
                "WHERE setting_key LIKE 'agent.%'").fetchall()
        return {str(r[0]): str(r[1]) for r in rows}
    except Exception:  # noqa: BLE001 - 表缺失/未建库/库锁都不该阻断 agent 主链路
        return {}


def _parse_override(field: str, raw: str) -> Any:
    """覆盖值字符串 → 该字段的类型。解析不出来返回 :data:`_INVALID`。

    写入侧（``SettingsStore.put``）已经做过校验，这里再兜一次是因为
    ``app_setting`` 也可能被手工 SQL 改过 —— 一条脏数据只该让这一项回退默认，
    不该让整个 agent 起不来。
    """
    try:
        if field in ("default_skills", "default_mcp_tools"):
            return parse_capability_list(raw)
        if field == "timeout_seconds":
            # 区间与 capabilities.TIMEOUT_* 同源：脏数据（0 / -1 / 99999）只让
            # 这一项回退默认档，不扩大也不收窄真实语义。
            from lquant.agent.capabilities import (  # noqa: PLC0415
                TIMEOUT_MAX_SECONDS,
                TIMEOUT_MIN_SECONDS,
            )

            val = int(float(raw))
            return val if TIMEOUT_MIN_SECONDS <= val <= TIMEOUT_MAX_SECONDS else _INVALID
        if field == "max_concurrent_runs":
            from lquant.agent.concurrency import (  # noqa: PLC0415
                MAX_RUNS_MAX,
                MAX_RUNS_MIN,
            )

            val = int(float(raw))
            return val if MAX_RUNS_MIN <= val <= MAX_RUNS_MAX else _INVALID
        if field in ("skip_permissions", "partial_messages"):
            low = raw.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            return _INVALID
        if field == "provider":
            return raw.strip() or _INVALID
    except (TypeError, ValueError):
        return _INVALID
    return _INVALID


def effective_agent_config() -> dict[str, Any]:
    """合并后的 agent 配置（含运行时覆盖）。调用方按字段名取值。"""
    base = get_settings().agent
    out: dict[str, Any] = {field: getattr(base, field) for field in RUNTIME_KEYS}
    raw = _raw_overrides()
    for field, key in RUNTIME_KEYS.items():
        if key not in raw:
            continue
        val = _parse_override(field, raw[key])
        if val is not _INVALID:
            out[field] = val
    return out
