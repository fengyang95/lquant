#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT

# 1) Redis（RQ 队列）
if command -v docker >/dev/null 2>&1; then
  docker compose up -d redis
else
  command -v redis-server >/dev/null 2>&1 && (redis-server --daemonize yes || true)
fi

# 2) API
.venv/bin/uvicorn lquant.server.main:app --reload --port 8000 &
# 3) Worker（default / ingest / backtest / mining 四队列）
# lquant-mining 承载因子评价与因子挖掘：此前注释写了 factor 却漏订阅该队列，
# Redis 模式下这两个任务会永远停在 queued（并因评价的确定性 job_id 把该因子
# 永久 409 锁死）。
.venv/bin/rq worker lquant-default lquant-ingest lquant-backtest lquant-mining \
  --url "${LQ_REDIS_URL:-redis://localhost:6379/0}" &

# 4) Web
if [ -d web/node_modules ]; then (cd web && npm run dev &) ; fi

echo "API    http://localhost:8000/docs"
echo "Web    http://localhost:3000"
wait
