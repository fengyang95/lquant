"""运行时配置（settings）：默认值 + config 派生 + 用户覆盖，读写 app_setting 表。

覆盖层（低→高）：
  代码默认 SETTING_DEFS  <  config/*.yaml 派生值  <  app_setting 表（用户 PUT 写入，source=runtime）
「没配置」不是错误：任何 key 都有 default，合并后总返回可用的最终值。
写必须过白名单与类型校验（coerce），未知 key / 非法值 → 直接失败，绝不静默吞掉。
"""
from __future__ import annotations

from dataclasses import dataclass


# key -> 定义。type: str|bool|enum|list
@dataclass(frozen=True)
class SettingDef:
    default: object
    ty: str = "str"
    choices: tuple = ()
    label: str = ""


SETTING_DEFS: dict[str, SettingDef] = {
    "providers_order": SettingDef(default=(), ty="list", label="数据源优先级（越靠前越优先）"),
    "factor_cache_enabled": SettingDef(default=True, ty="bool", label="因子两级缓存"),
    "rebalance_default": SettingDef(
        default="monthly", ty="enum",
        choices=("daily", "weekly", "monthly", "none"), label="回测默认调仓频率"),
    "price_mode_default": SettingDef(
        default="next_open", ty="enum",
        choices=("next_open", "next_vwap", "next_close", "same_close"),
        label="回测默认撮合模式"),
    "timezone": SettingDef(default="Asia/Shanghai", ty="str", label="时区"),
    "crosscheck_peers": SettingDef(default=(), ty="list", label="对拍 peer 源（跨源印证）"),
    "agent.provider": SettingDef(
        default="claude_code", ty="enum", choices=("claude_code", "codex", "mock"),
        label="问 AI 后端 provider（claude_code=内置 Claude Code；codex=OpenAI Codex CLI；"
              "mock=脚本化演示，不调 LLM）"),
    # 下面五项都是「新建会话的默认值 / 运行参数」，改完**下一次运行即生效**：
    # agent 侧一律经 lquant.agent.runtime.effective_agent_config() 取值，不缓存。
    "agent.default_skills": SettingDef(
        default="", ty="str",
        label="新建会话默认 skill（all=全开；none=一个都不启用；其余逗号分隔）"),
    "agent.default_mcp_tools": SettingDef(
        default="", ty="str",
        label="新建会话默认 MCP 工具（同上：all / none / 逗号分隔）"),
    "agent.timeout_seconds": SettingDef(
        default=300, ty="int", label="问 AI 单次回答超时（秒，超时 kill CLI 子进程）"),
    "agent.skip_permissions": SettingDef(
        default=True, ty="bool",
        label="CLI 全自主权限（无头运行必需：claude --dangerously-skip-permissions / "
              "codex 同时关闭沙箱；关掉后 codex 侧 MCP 工具不可用）"),
    "agent.partial_messages": SettingDef(
        default=True, ty="bool",
        label="claude token 级流式（--include-partial-messages；老版本 CLI 不认这个旗标时关掉）"),
    "agent.max_concurrent_runs": SettingDef(
        default=4, ty="int",
        label="同时在跑的 agent 上限（每个回答都是一个全自主权限的 CLI 子进程，超限的新请求直接拒绝）"),
    "coverage_drop_warn_pct": SettingDef(
        default=30, ty="int", label="覆盖度环比下降告警阈值（%，前端标橙线）"),
}


def _coerce(ty: str, raw: str, choices: tuple) -> str:
    """把字符串依类型规范化；非法直接抛 ValueError（快失败）。"""
    if ty == "bool":
        if raw.strip().lower() in ("true", "1", "yes", "on"):
            return "true"
        if raw.strip().lower() in ("false", "0", "no", "off"):
            return "false"
        raise ValueError(f"布尔值应为 true/false，收到 {raw!r}")
    if ty == "int":
        int(raw)
        return str(int(raw))
    if ty == "list":
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        if not vals:
            raise ValueError("列表不能为空")
        return ",".join(vals)
    if ty == "enum":
        if raw not in choices:
            raise ValueError(f"取值必须 ∈ {sorted(choices)}，收到 {raw!r}")
        return raw
    return raw


def coerce_setting(key: str, raw: str) -> tuple[str, str | None]:
    """合法返回 (规范化值, None)；非法返回 (raw, 错误)。"""
    d = SETTING_DEFS.get(key)
    if d is None:
        return raw, f"未知配置项 {key!r}"
    try:
        return _coerce(d.ty, raw, d.choices), None
    except ValueError as e:
        return raw, str(e)


def _serialize(defn: SettingDef, value) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return "true" if value is True else "false" if value is False else str(value)


def _parse(defn: SettingDef, s: str):
    if defn.ty == "bool":
        return s == "true"
    if defn.ty == "list":
        return [v for v in s.split(",") if v] if s else []
    return s


def _config_default_providers() -> tuple[str, ...]:
    """从 config/providers.yaml 取启用源优先级（source=config）。"""
    try:
        from lquant.core.config import get_settings

        raw = get_settings().raw.get("providers", []) or []
        return tuple(p["name"] for p in raw if p.get("enabled"))
    except Exception:  # noqa: BLE001 - 读不到配置就回退默认空
        return ()


def _config_default_agent(key: str) -> tuple[object, str]:
    """config/app.yaml **显式声明**的 ``agent.<field>`` → (值, "config")。

    没声明（或读不到）就回退代码默认值，source 标 "default" —— 之前是拿
    解析结果跟写死的 "mock" 比，默认值一改语义就错位，这里改成看原始 yaml 键。

    取原始 yaml 而不是 ``get_settings().agent``：后者是**解析后**的形态
    （``all`` 已经变成 ``None``），展示给前端时「当前值 all」比「空串」清楚得多。
    """
    field = key.split(".", 1)[1]
    default = SETTING_DEFS[key].default
    try:
        from lquant.core.config import get_settings  # noqa: PLC0415

        raw = get_settings().raw.get("agent") or {}
        declared = raw.get(field)
        if declared is not None:
            return declared, "config"
    except Exception:  # noqa: BLE001 - 读不到配置就回退默认
        pass
    return default, "default"


def defaults() -> dict[str, tuple[object, str]]:
    """key -> (value, source)。providers_order 与 agent.* 从 config 派生。"""
    out: dict[str, tuple[object, str]] = {}
    for k, d in SETTING_DEFS.items():
        if k == "providers_order":
            cfg = _config_default_providers()
            out[k] = (cfg, "config" if cfg else "default")
        elif k.startswith("agent."):
            out[k] = _config_default_agent(k)
        else:
            out[k] = (d.default, "default")
    return out


class SettingsStore:
    """读写 app_setting。自动建表；读时合并默认，写时过白名单。"""

    def __init__(self, *, table: str = "app_setting") -> None:
        self.table = table

    def _ensure_table(self) -> None:
        from lquant.core.db import writer
        from lquant.data.store.ddl import DDL_STATEMENTS

        with writer() as con:
            stmt = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS app_setting" in s)
            con.execute(stmt)

    def all(self) -> list[dict]:
        self._ensure_table()
        merged = dict(defaults())
        overrides: dict[str, str] = {}
        try:
            from lquant.core.db import reader

            with reader() as con:
                rows = con.execute(
                    f'SELECT setting_key, setting_value FROM "{self.table}"').fetchall()
                overrides = {r[0]: r[1] for r in rows}
        except Exception:  # noqa: BLE001 - 表空/缺失时仅剩默认
            pass
        items = []
        for k, (dflt, src) in merged.items():
            defn = SETTING_DEFS[k]
            if k in overrides:
                val, err = coerce_setting(k, overrides[k])
                value = _parse(defn, val) if err is None else defn.default
                src = "runtime"
            else:
                value = dflt
            items.append({"key": k, "value": value, "type": defn.ty,
                          "source": src, "label": defn.label,
                          "choices": list(defn.choices) or None})
        return items

    def put(self, key: str, value: str) -> dict:
        self._ensure_table()
        if key not in SETTING_DEFS:
            raise ValueError(f"未知配置项 {key!r}")
        coerced, err = coerce_setting(key, value)
        if err:
            raise ValueError(err)
        self._validate_extra(key, coerced)
        from lquant.core.db import writer

        with writer() as con:
            con.execute(
                f'INSERT INTO "{self.table}" (setting_key, setting_value, source, updated_at) '
                "VALUES (?, ?, 'runtime', now()) "
                f'ON CONFLICT (setting_key) DO UPDATE SET setting_value = excluded.setting_value, '
                "source = 'runtime', updated_at = now()",
                [key, coerced])
        self._invalidate_provider_cache(key)
        return {"key": key, "value": _parse(SETTING_DEFS[key], coerced)}

    @staticmethod
    def _invalidate_provider_cache(key: str) -> None:
        """影响 provider 链的配置写入后立刻失效 build_chain 缓存。

        providers_order 决定 Fallback 链顺序（首位 = 主源），crosscheck_peers
        决定对拍 peer。不失效的话 lru_cache 会让「保存并立即生效」变成
        「重启才生效」——设计契约要求改完下一次拉取即生效。
        """
        if key not in ("providers_order", "crosscheck_peers"):
            return
        from lquant.data.providers import reset_chain

        reset_chain()

    def _validate_extra(self, key: str, coerced: str) -> None:
        """类型之外的业务校验（key 特有），非法 → ValueError。

        crosscheck_peers：只接受已注册且声明 daily/etf_daily 的源。
        校验器在 data 层（源注册表/capability 归 data 域），此处延迟导入 ——
        与 _ensure_table 延迟导入 lquant.data.store.ddl 同方向、同理由。
        agent.*：能力名单与超时都按 agent 域的注册表校验，同样延迟导入。
        """
        if key == "crosscheck_peers":
            from lquant.data.ingest.crosscheck import validate_peers

            validate_peers([v for v in coerced.split(",") if v])
            return
        if key in ("agent.default_skills", "agent.default_mcp_tools"):
            self._validate_agent_capability(key, coerced)
            return
        if key == "agent.timeout_seconds":
            from lquant.agent.capabilities import (  # noqa: PLC0415
                CapabilityError,
                clean_timeout_seconds,
            )

            try:
                clean_timeout_seconds(coerced)
            except CapabilityError as e:
                raise ValueError(str(e)) from e
            return
        if key == "agent.max_concurrent_runs":
            # 区间与 agent.concurrency.MAX_RUNS_* 同源：写入侧与运行时兜底用
            # 同一份边界，杜绝「设置页存不进去但接口收下了」这类分叉。
            from lquant.agent.concurrency import MAX_RUNS_MAX, MAX_RUNS_MIN  # noqa: PLC0415

            val = int(float(coerced))
            if not MAX_RUNS_MIN <= val <= MAX_RUNS_MAX:
                raise ValueError(
                    f"并发上限应在 {MAX_RUNS_MIN}~{MAX_RUNS_MAX} 之间，收到 {val}")

    @staticmethod
    def _validate_agent_capability(key: str, coerced: str) -> None:
        """``all`` / ``none`` / 逗号名单。名单里的名字必须**真的存在**（skill 目录
        或 MCP 工具注册表）—— 默认能力集是「以后每次新建会话都照这个来」，
        写错一个名字会让新建的会话静默少一项能力，用户只会觉得「它怎么不会用」。
        """
        from lquant.core.config import parse_capability_list  # noqa: PLC0415

        names = parse_capability_list(coerced)
        if names is None:  # all / 空白 = 全开
            return
        if not names:
            return  # none = 一个都不启用
        if key == "agent.default_mcp_tools":
            from lquant.agent.capabilities import known_mcp_tools  # noqa: PLC0415

            unknown = sorted(set(names) - known_mcp_tools())
            if unknown:
                raise ValueError(f"未知 MCP 工具: {', '.join(unknown)}")
            return
        from lquant.agent.capabilities import (  # noqa: PLC0415
            CapabilityError,
            check_default_skill_names,
        )

        try:
            check_default_skill_names(names)
        except CapabilityError as e:
            raise ValueError(str(e)) from e

    def reset(self, key: str) -> None:
        self._ensure_table()
        from lquant.core.db import writer

        with writer() as con:
            con.execute(f'DELETE FROM "{self.table}" WHERE setting_key = ?', [key])
        self._invalidate_provider_cache(key)