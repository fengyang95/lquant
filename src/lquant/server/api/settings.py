"""运行时配置 API（settings 页）：GET 全量配置 + PUT 逐项改写，走统一封套。

设计契约 4.API 契约 → /settings：配置（数据源优先级 / 规则表 / 缓存）。
覆盖层低→高：代码默认 < config/*.yaml 派生 < app_setting 表（用户覆盖）。
写必须过白名单 + 类型校验；未知 key / 非法值 → 422 明示，不静默。
"""
from __future__ import annotations

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from lquant.core.settings_store import SettingsStore
from lquant.server.envelope import make_router

router = make_router(prefix="/settings", tags=["settings"])


class SettingItem(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=500)


@router.get("")
def get_all_settings() -> list[dict]:
    """全量配置（含 label/type/choices/source），前端 settings 页一键渲染。"""
    return SettingsStore().all()


@router.put("/{key}")
def put_setting(key: str, body: SettingItem) -> dict:
    if body.key != key:
        raise HTTPException(422, f"路径与 body 的 key 不一致（{key} vs {body.key}）")
    try:
        return SettingsStore().put(key, body.value)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.delete("/{key}")
def reset_setting(key: str) -> dict:
    """重置回默认（删除用户覆盖）。未知 key 返回 404。"""
    from lquant.core.settings_store import SETTING_DEFS

    if key not in SETTING_DEFS:
        raise HTTPException(404, f"未知配置项 {key!r}")
    SettingsStore().reset(key)
    return {"key": key, "reset": True}


@router.get("/providers")
def providers() -> list[dict]:
    """数据源优先级 + 启用状态（settings 页拖排序）。读 providers.yaml 而非 app.yaml。"""
    from lquant.core.config import load_providers

    raw = load_providers().get("providers", []) or []
    return [{"name": p.get("name"), "enabled": bool(p.get("enabled")),
             "capability": list(p.get("capability") or []), "note": p.get("note")}
            for p in raw]