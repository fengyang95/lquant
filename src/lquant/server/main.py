"""服务入口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lquant.core.config import get_settings
from lquant.server import ws
from lquant.server.api import (
    backtests,
    data,
    factors,
    health,
    market,
    paper,
    strategies,
    sync,
    watchlist,
)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="lquant", version="0.1.0",
                  description="A股量化研究平台 API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )
    for r in (health, data, factors, backtests, market, paper, watchlist, strategies, sync):
        app.include_router(r.router, prefix="/api")
    app.include_router(ws.router)  # /ws/jobs/{id}，无 /api 前缀（与前端代理一致）
    return app


app = create_app()


@app.on_event("startup")
def _startup() -> None:
    from lquant._rust.loader import print_status

    print_status()
    # 结构迁移：老库的 factor_def 缺 expression 列会让因子 API 静默丢数据
    try:
        from lquant.core.db import writer
        from lquant.data.store.ddl import (
            DDL_STATEMENTS,
            ensure_classify_snapshots,
            ensure_collect_log,
            ensure_factor_def,
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
    except Exception as e:  # noqa: BLE001 - 库未初始化时不应阻断服务启动
        print(f"[startup] schema 迁移跳过: {e}")

    # 定时同步 worker（daemon 线程，每 30s 检查到期作业；测试用 LQ_SYNC_WORKER=0 关闭）
    import os
    import threading

    if os.getenv("LQ_SYNC_WORKER", "1") != "0":
        def _sync_worker() -> None:
            from lquant.sync import manager

            try:
                n = manager.seed_defaults()
                if n:
                    print(f"[sync] 已种子 {n} 个默认同步作业")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] sync 作业种子失败: {e}")
            manager.loop_forever(interval=30)

        threading.Thread(target=_sync_worker, name="sync-worker", daemon=True).start()
