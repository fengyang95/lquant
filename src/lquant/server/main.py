"""服务入口。"""
from __future__ import annotations

import duckdb
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lquant.core.logging import setup_logging
from lquant.monitor import start_monitor, stop_monitor
from lquant.monitor.api_mw import MonitorMiddleware
from lquant.server import ws
from lquant.server.api import (
    a2a,
    agent,
    analyses,
    ask,
    backtests,
    data,
    data_admin,
    etf,
    factors,
    fundamental,
    health,
    market,
    ml,
    monitor,
    news,
    paper,
    qlib,
    security,
    settings,
    strategies,
    sync,
    task_center,
    watchlist,
)


def create_app() -> FastAPI:
    # 进程入口统一接线：server（uvicorn）与 sync 调度 worker 共用本进程日志
    setup_logging()
    app = FastAPI(title="lquant", version="0.1.0",
                  description="A股量化研究平台 API")

    @app.exception_handler(duckdb.IOException)
    async def _duckdb_lock_to_503(request: Request, exc: duckdb.IOException) -> JSONResponse:  # noqa: ARG001
        """跨进程写锁冲突 → 503（可重试语义）而非裸 500。

        _connect 已做有界重试；到这里说明持锁方长时间不释放，前端可提示
        「数据湖忙，请稍后重试」。非锁冲突的 IOException 原样 500（重新抛出
        走 starlette 默认错误页）。
        """
        if "lock" not in str(exc).lower():
            raise exc
        return JSONResponse(status_code=503,
                            content={"detail": "数据湖忙：另一进程持有 DuckDB 写锁，请稍后重试"})
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )
    for r in (health, data, data_admin, factors, backtests, market, paper, watchlist,
              strategies, analyses, sync, etf, news, settings, ask, agent,
              qlib, task_center, monitor, fundamental, ml, security):
        app.include_router(r.router, prefix="/api")
    app.include_router(ws.router)  # /ws/jobs/{id}，无 /api 前缀（与前端代理一致）
    # A2A：Agent Card 按 RFC 8615 挂在 /.well-known/，POST 落在 /a2a —— 两者都
    # **不能**吃上面的 /api 前缀，所以单独 include（外部 A2A 客户端按规范位发现）。
    app.include_router(a2a.router)

    @app.on_event("startup")
    def _a2a_startup_warning() -> None:
        a2a.warn_if_unauthenticated()

    @app.on_event("startup")
    def _monitor_startup() -> None:
        start_monitor()
        # 默认同步作业播种（幂等）：每个 app 实例的 startup 都要执行 ——
        # 只挂在模块级 _startup 上的话，TestClient(create_app()) 这类新建
        # 实例不会播种，GET /sync/jobs 拿到空列表。
        try:
            from lquant.sync import manager

            n = manager.seed_defaults()
            if n:
                print(f"[sync] 已种子 {n} 个默认同步作业")
        except Exception as e:  # noqa: BLE001 - 播种失败不挡启动
            print(f"[warn] sync 作业种子失败: {e}")

    @app.on_event("shutdown")
    async def _monitor_shutdown() -> None:
        stop_monitor()
        # 除监控外，还要收掉 agent 会话存储：它是 aiosqlite，worker 线程 non-daemon，
        # 只有 await close() 才停得掉。不关的话 uvicorn 优雅停机会卡在解释器退出
        # 阶段（进程不退出），pytest 收尾同理。
        from lquant.agent.service import shutdown_agent_service  # noqa: PLC0415

        await shutdown_agent_service()

    return MonitorMiddleware(app)


app = create_app()
# MonitorMiddleware 是纯 ASGI 包裹，没有 on_event；模块级既有 startup 钩子仍需
# 注册到底层 FastAPI 实例上（uvicorn 的 "main:app" 指向包了中间件的 ASGI 栈）。
_fastapi_app = app.app if isinstance(app, MonitorMiddleware) else app


@_fastapi_app.on_event("startup")
def _startup() -> None:
    from lquant._rust.loader import print_status

    print_status()
    # 重启遗留的队列任务标记 interrupted（WS 兜底链据此发终态帧，
    # 前端显示「已中断」而非「连接中断」）
    try:
        from lquant.server.jobs import mark_interrupted_jobs

        n = mark_interrupted_jobs()
        if n:
            print(f"[startup] {n} 个遗留任务标记 interrupted")
    except Exception:  # noqa: BLE001 - 标记失败不挡启动
        pass
    # 结构迁移：老库的 factor_def 缺 expression 列会让因子 API 静默丢数据
    try:
        from lquant.core.db import writer
        from lquant.data.store.ddl import (
            DDL_STATEMENTS,
            ensure_classify_snapshots,
            ensure_collect_log,
            ensure_factor_def,
            ensure_factor_def_columns,
            ensure_ml_run_columns,
        )

        with writer() as con:
            for stmt in DDL_STATEMENTS:
                con.execute(stmt)
            if ensure_factor_def(con):
                print("[migrate] factor_def → expression/description 结构")
            if ensure_collect_log(con):
                print("[migrate] collect_log → 补主键")
            if ensure_classify_snapshots(con):
                print("[migrate] industry_classify → 补 (symbol, std_date) 主键")
            if ensure_factor_def_columns(con):
                print("[migrate] factor_def → 补 source/source_ref/factor_id 列")
            if ensure_ml_run_columns(con):
                print("[migrate] ml_run → 补 model_name/model_version/artifact 列")
    except Exception as e:  # noqa: BLE001 - 库未初始化时不应阻断服务启动
        print(f"[startup] schema 迁移跳过: {e}")

    # 数据任务中断标记：重启后 pending/running 残留标 interrupted（可 retry 续传），
    # 只 log 不阻塞启动 —— 库未初始化时这里查不到表也无妨。
    try:
        from lquant.data.ingest.tasks import mark_interrupted_on_startup

        n = mark_interrupted_on_startup()
        if n:
            print(f"[startup] {n} 个数据任务标记为 interrupted（可 retry 续传）")
    except Exception as e:  # noqa: BLE001 - 同上，不阻断启动
        print(f"[startup] 数据任务中断标记跳过: {e}")

    # 资讯任务启动恢复：建 news_task 表后把 pending/running 残留标 interrupted
    try:
        from lquant.core.db import writer
        from lquant.news.tasks import (
            init_news_task_ddl,
            mark_interrupted_on_startup,
        )

        with writer() as con:
            init_news_task_ddl(con)
            n = mark_interrupted_on_startup(con)
        if n:
            print(f"[startup] {n} 个资讯任务标记为 interrupted（可 retry 续传）")
    except Exception as e:  # noqa: BLE001 - 同上，不阻断启动
        print(f"[startup] 资讯任务中断标记跳过: {e}")

    # 定时同步 worker（daemon 线程，每 30s 检查到期作业；测试用 LQ_SYNC_WORKER=0 关闭）
    import os
    import threading

    if os.getenv("LQ_SYNC_WORKER", "1") != "0":
        def _sync_worker() -> None:
            from lquant.sync import manager

            manager.loop_forever(interval=30)

        threading.Thread(target=_sync_worker, name="sync-worker", daemon=True).start()

        def _startup_backfill() -> None:
            """部署/重启后自动检查 index_daily 缺失并补齐（backfill 作业之外的兜底）。"""
            import time as _time

            _time.sleep(10)  # 等库与日历源就绪，避开启动风暴
            from lquant.market.backfill import ensure_market_coverage

            try:
                res = ensure_market_coverage(days=90)
                if res.get("missing_before"):
                    print(f"[startup] 大盘数据补齐: "
                          f"补 {res.get('persisted', 0)} 行 "
                          f"({list(res['missing_before'])})")
            except Exception as e:  # noqa: BLE001 - 启动兜底失败不阻断服务
                print(f"[warn] 启动补齐失败（可用 POST /api/market/backfill 手动触发）: {e}")

        threading.Thread(target=_startup_backfill, name="startup-backfill",
                         daemon=True).start()
