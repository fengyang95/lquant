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


def defaults() -> dict[str, tuple[object, str]]:
    """key -> (value, source)。providers_order 从 config 派生（config/默认）。"""
    out: dict[str, tuple[object, str]] = {}
    for k, d in SETTING_DEFS.items():
        if k == "providers_order":
            cfg = _config_default_providers()
            out[k] = (cfg, "config" if cfg else "default")
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
        from lquant.core.db import writer

        with writer() as con:
            con.execute(
                f'INSERT INTO "{self.table}" (setting_key, setting_value, source, updated_at) '
                "VALUES (?, ?, 'runtime', now()) "
                f'ON CONFLICT (setting_key) DO UPDATE SET setting_value = excluded.setting_value, '
                "source = 'runtime', updated_at = now()",
                [key, coerced])
        return {"key": key, "value": _parse(SETTING_DEFS[key], coerced)}

    def reset(self, key: str) -> None:
        self._ensure_table()
        from lquant.core.db import writer

        with writer() as con:
            con.execute(f'DELETE FROM "{self.table}" WHERE setting_key = ?', [key])