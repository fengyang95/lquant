"""通知 API：告警规则 CRUD / DryRun 评估 / 手动测试发送。

规则是数据（SQLite），API 只是薄封装 —— 评估逻辑全部在 notify.rules，
CLI 与 API 共享同一套口径，不会出现「两边判得不一样」。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lquant.notify import rules as rule_mod
from lquant.notify.rules import AlertRule, get_store, run_rules

router = APIRouter(prefix="/notify", tags=["notify"])


class RuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    target_scope: str = "single_symbol"
    target: str = ""
    alert_type: str = "price_above"
    parameters: dict = Field(default_factory=dict)
    severity: str = "warning"
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=0, le=86_400)


class RulePatch(BaseModel):
    name: str | None = None
    target_scope: str | None = None
    target: str | None = None
    alert_type: str | None = None
    parameters: dict | None = None
    severity: str | None = None
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86_400)


class EvaluateIn(BaseModel):
    """DryRun 请求体：行情上下文由调用方给（服务端不替调用方拉行情）。"""

    ctx: dict = Field(..., description='如 {"symbol":"600000.SH","last_price":10.5,"pre_close":10.0}')


class TestSendIn(BaseModel):
    title: str = "lquant notify test"
    text: str = "这是一条 lquant 通知测试消息"
    category: str = "report"
    severity: str = "info"


@router.get("/rules")
def list_rules():
    return {"rules": [rule_mod.rule_to_dict(r) for r in get_store().list()]}


@router.post("/rules")
def create_rule(body: RuleIn):
    rule = AlertRule(**body.model_dump())
    try:
        return rule_mod.rule_to_dict(get_store().add(rule))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: int, body: RulePatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail="无可更新字段")
    try:
        return rule_mod.rule_to_dict(get_store().update(rule_id, **fields))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int):
    if not get_store().delete(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"deleted": rule_id}


@router.post("/rules/{rule_id}/evaluate")
def evaluate_rule(rule_id: int, body: EvaluateIn):
    """DryRun：只判不记不发。冷却中的规则照常报 cooldown —— 状态即真相。"""
    rule = get_store().get(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    status, detail = rule_mod.evaluate(rule, body.ctx)
    return {"rule_id": rule_id, "status": status, "detail": detail}


@router.post("/rules/run")
def run_all_rules(ctxs: list[dict]):
    """批量评估全部启用规则；命中即发通知并落冷却（非 DryRun）。"""
    return {"results": run_rules(ctxs)}


@router.post("/test")
def test_send(body: TestSendIn):
    """手动测试发送：走与告警完全相同的 notify 主链路（含路由/降噪）。"""
    from lquant.notify import format_results, notify
    results = notify(body.title, body.text, category=body.category,
                     severity=body.severity)
    return {"summary": format_results(results),
            "results": [r.__dict__ for r in results]}
