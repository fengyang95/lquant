"""任务管理中心 API。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from lquant.data.ingest import tasks as ingest_tasks
from lquant.server.jobs import (
    _redis_available,
    get_job,
    list_recent_jobs,
    request_cancel,
)

router = APIRouter(prefix="/tasks", tags=["task-center"])

KINDS = ("data", "sync", "backtest", "factor", "qlib", "ml")

# 各自原生状态 → 统一 state（queued/running/finished/failed/canceled）
_DATA_STATE = {
    "pending": "queued", "running": "running",
    "ok": "finished", "partial": "finished",
    "failed": "failed", "interrupted": "canceled",
}
_SYNC_STATE = {"ok": "finished", "failed": "failed", "running": "running"}
_JOB_STATE = {
    "queued": "queued", "enqueued": "queued", "deferred": "queued", "started": "running",
    "finished": "finished", "failed": "failed", "canceled": "canceled",
}
_QUEUE_OF_KIND = {"backtest": "lquant-backtest", "factor": "lquant-mining",
                  "qlib": "lquant-qlib", "ml": "lquant-ml"}

#: 队列任务的兜底显示名（任务体没登记名字时用）
_DEFAULT_JOB_NAME = {"backtest": "参数扫描", "factor": "因子挖掘",
                     "qlib": "Qlib 任务", "ml": "ML 训练"}


def _ts(v) -> float:
    """created_at 归一到可排序时间戳：float 直用，字符串尽力解析，失败为 0。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _data_items(limit: int) -> list[dict]:
    """数据任务（data_task）归一。"""
    out = []
    for t in ingest_tasks.list_tasks(limit):
        state = _DATA_STATE.get(t["status"], "finished")
        out.append({
            "id": t["task_id"], "kind": "data",
            "name": f"数据任务·{t['kind']}",
            "status": t["status"], "state": state,
            "created_at": str(t.get("started_at") or t.get("finished_at") or ""),
            "params": t.get("params") or {},
            "error": t.get("message") if state == "failed" else None,
        })
    return out


def _sync_items(limit: int) -> list[dict]:
    """同步作业运行历史（sync_run）归一。"""
    from lquant.sync import manager

    return [{"id": r["run_id"], "kind": "sync", "name": r["job_name"],
             "status": r["status"], "state": _SYNC_STATE.get(r["status"], "queued"),
             "created_at": r["started_at"],
             "params": {"sync_id": r["sync_id"], "kind": r["kind"], "rows": r.get("rows")}}
            for r in manager.history(limit)]


def _job_items(queue: str, kind: str, limit: int) -> list[dict]:
    """队列任务（backtest 扫描 / factor 挖掘 / qlib 工作流 / ml 训练）归一。

    进度取自进度注册表（enqueue 时任务体声明 progress 回调才会写入），
    显示名优先取登记名（因子评价 / 参数扫描 / ML 训练）。
    """
    from lquant.server.progress import get_job_name, get_progress

    out = []
    for j in list_recent_jobs(limit):
        if j.get("queue") != queue:
            continue
        status = j.get("status") or "queued"
        jid = j["id"]
        out.append({"id": jid, "kind": kind,
                    "name": get_job_name(jid) or _DEFAULT_JOB_NAME.get(kind, "因子挖掘"),
                    "status": status, "state": _JOB_STATE.get(status, "queued"),
                    "created_at": j.get("created_at", 0.0),
                    "params": {}, "error": j.get("error"),
                    "progress": get_progress(jid)})
    return out


def _factor_items(limit: int) -> list[dict]:
    """factor 类目：队列任务 + 回填落库元信息。

    队列任务本身不带参数（jobs.py 只记 id/name/queue/status），不回填的话 UI 只能
    看到一个 `factor-e…` 短 id —— 既分不清是评价还是挖掘，也不知道在评哪个因子。
    subtype 由落库位置判定（job_results ⇒ 评价，factor_mining_run ⇒ 挖掘），
    比依赖可自由改写的显示名可靠（原先靠 `name === '因子评价'` 字符串比对）。
    """
    items = _job_items("lquant-mining", "factor", limit)
    ids = [it["id"] for it in items]
    if not ids:
        return items

    from lquant.server.eval_results import meta_by_ids

    try:
        eval_meta = meta_by_ids(ids)
        mine_meta: dict[str, dict] = {}
        rest = [i for i in ids if i not in eval_meta]
        if rest:
            from lquant.server.api.factors import mining_meta_by_ids

            mine_meta = mining_meta_by_ids(rest)
    except Exception as e:  # noqa: BLE001 - 回填是尽力而为，不能拖垮整个任务列表
        from loguru import logger

        logger.warning(f"任务中心回填 factor 参数失败（列表降级为无参数）: {e}")
        return items

    for it in items:
        em = eval_meta.get(it["id"])
        if em is not None and em.get("kind") == "factor_eval":
            it["subtype"] = "factor_eval"
            it["params"] = em.get("params") or {}
            continue
        mm = mine_meta.get(it["id"])
        if mm is not None:
            it["subtype"] = "factor_mine"
            it["params"] = mm
    return items


def _summary_of(items: list[dict]) -> dict:
    def _c(state: str) -> int:
        return sum(1 for x in items if x["state"] == state)

    return {"total": len(items), "running": _c("running"), "failed": _c("failed"),
            "succeeded": _c("finished"), "canceled": _c("canceled")}


def _items(kind: str, limit: int) -> list[dict]:
    """按 kind 收集归一任务列表；未知类别 422。"""
    if kind == "data":
        return _data_items(limit)
    if kind == "sync":
        return _sync_items(limit)
    if kind == "backtest":
        return _job_items("lquant-backtest", "backtest", limit)
    if kind == "factor":
        return _factor_items(limit)
    if kind == "qlib":
        return _job_items("lquant-qlib", "qlib", limit)
    if kind == "ml":
        return _job_items("lquant-ml", "ml", limit)
    raise HTTPException(422, f"未知任务类别: {kind!r}（可选 {KINDS}）")


@router.get("")
def list_tasks_ep(
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """各类任务统一列表：{id, kind, name, status, state, created_at, params}，
    按 created_at 倒序，kind 可选过滤。"""
    if kind is not None and kind not in KINDS:
        raise HTTPException(422, f"未知任务类别: {kind!r}（可选 {KINDS}）")
    wanted = [kind] if kind else list(KINDS)
    items: list[dict] = []
    for k in wanted:
        items.extend(_items(k, limit))
    items.sort(key=lambda x: _ts(x["created_at"]), reverse=True)
    return items[:limit]


@router.get("/summary")
def summary_ep() -> dict:
    """{kinds: {kind: {total, running, failed, succeeded, canceled}}}，
    与 GET /api/tasks 同一收集口径（每类聚合前 200 条）。"""
    kinds: dict[str, dict] = {}
    for k in KINDS:
        kinds[k] = _summary_of(_items(k, 200))
    return {"kinds": kinds}


class RetryIn(BaseModel):
    params: dict | None = None


@router.post("/{kind}/{task_id}/retry", status_code=202)
def retry_ep(kind: str, task_id: str, req: RetryIn | None = None) -> dict:
    """retry：data 任务可携带新参数（claim_retry 合并覆盖存储参数后重新入队执行）。

    202 + task_id；409 不可 retry / 竞争抢先；422 状态或参数不允许；404 不存在。
    """
    if kind != "data":
        raise HTTPException(422, f"kind={kind} 暂不支持 retry（仅 data）")
    task = ingest_tasks.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    try:
        # allow_finished：任务中心「重跑（可改配置）」语义，已完成任务也可重跑
        ingest_tasks.claim_retry(task_id, req.params if req else None,
                                 allow_finished=True)
    except ingest_tasks.TaskConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    from lquant.server.jobs import enqueue

    # job_id = task_id：cancel 探针按 task_id 找取消目标（与 data.py 入队一致）
    enqueue("lquant-ingest", ingest_tasks.run_claimed_task, task_id,
            job_id=task_id)
    return {"task_id": task_id, "status": "running", "params": task["params"]}


@router.post("/{kind}/{task_id}/cancel")
def cancel_ep(kind: str, task_id: str) -> dict:
    """cancel：请求取消任务（协作式：任务体轮询探针提前收尾）。

    kind=data：data_task 必须存在且 pending/running 才可取消 —— 队列侧的
    取消目标以 task_id 入队登记（enqueue(job_id=task_id)，本地降级模式
    LocalJob.id == task_id，request_cancel 置标记，run_claimed_task 批间
    轮询探针收尾为 interrupted）。RQ 模式下运行中的 job 无法强杀，与
    backtest/factor 同样返回 409。
    200 + {canceled: true}；404 不存在；409 已结束/不可取消。
    """
    if kind == "data":
        task = ingest_tasks.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        if task["status"] not in ("pending", "running"):
            raise HTTPException(409, f"任务 {task_id} 已结束（{task['status']}），不可取消")
        if not request_cancel(task_id):
            # 孤儿 pending：本地降级模式下注册表已随创建它的进程消失，
            # 任务永远不会被执行 —— 标记 interrupted 解除对后续任务的阻塞
            # （等价启动标记语义；RQ 模式下 job 可能在 Redis 队列里，不在此越权）
            if (not _redis_available() and task["status"] == "pending"
                    and ingest_tasks.mark_canceled_pending(task_id)):
                return {"task_id": task_id, "canceled": True}
            raise HTTPException(409, f"任务 {task_id} 当前不可取消（队列目标不存在或运行中）")
        # RQ queued job 被真取消 → job 永不执行，data_task 停 pending 的坑：
        # 显式落 interrupted。本地模式线程稍后会跑并再收尾一次，状态一致。
        job = get_job(task_id)
        if job is not None and getattr(job, "get_status", lambda: None)() == "canceled":
            ingest_tasks.mark_canceled_pending(task_id)
        return {"task_id": task_id, "canceled": True}
    if get_job(task_id) is None:
        raise HTTPException(404, f"任务不存在或不可取消: {task_id}")
    if not request_cancel(task_id):
        raise HTTPException(409, f"任务 {task_id} 已结束或不可取消")
    return {"task_id": task_id, "canceled": True}
