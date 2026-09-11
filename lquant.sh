#!/usr/bin/env bash
# ============================================================================
# lquant 一键启动脚本
#
#   ./lquant.sh install     安装全部依赖（Python/Node/Redis 自动检测安装）
#   ./lquant.sh update [--full] [--no-restart]  同步更新前后端代码并使其生效
#   ./lquant.sh build       打包：前端生产构建 + 后端 wheel + 发行包
#   ./lquant.sh start       启动全栈（API + Worker + Web），后台常驻
#   ./lquant.sh all         install + build + start 一条龙
#   ./lquant.sh stop|status|restart|logs [api|web|worker]
#   ./lquant.sh bootstrap [--full]   拉取参考数据 + 日线（哨兵池/全市场）
#   ./lquant.sh doctor      环境体检
#
# update 说明：
#   后端为 editable 安装，代码改动天然生效，但 pyproject 依赖变化需重装；
#   前端需 npm install 同步依赖、next build 刷新生产构建；
#   运行中的服务是启动时加载的旧代码，最后自动重启使其吃到新代码。
#
# 特性：
#   - 国内镜像加速（清华 PyPI / npmmirror），可用环境变量覆盖
#   - 无 uv 自动装 uv，装不上自动降级 python3 -m venv + pip
#   - 无 Redis 不阻塞启动，任务队列自动降级为本地线程
#   - 无 cargo 自动装 rustup（装不上才跳过 Rust 扩展，降级 Python 参考实现）
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"   # uv 默认装在这里

# ----------------------------- 可配置项 ------------------------------------
PYPI_INDEX="${LQ_PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
NPM_REGISTRY="${LQ_NPM_REGISTRY:-https://registry.npmmirror.com}"
# rustup 国内镜像（rsproxy），海外环境可 LQ_RUSTUP_DIST_SERVER=https://static.rust-lang.org 覆盖
RUSTUP_DIST_SERVER="${LQ_RUSTUP_DIST_SERVER:-https://rsproxy.cn}"
RUSTUP_UPDATE_ROOT="${LQ_RUSTUP_UPDATE_ROOT:-https://rsproxy.cn/rustup/dist}"
API_PORT="${LQ_API_PORT:-8000}"
WEB_PORT="${LQ_WEB_PORT:-3000}"
# 默认只绑回环。API 无鉴权，且 /api/analyses、/api/backtests/run-code 等端点会
# exec 用户代码 —— 绑 0.0.0.0 等于把 RCE 开放给同网段。确需对外时显式设
# LQ_API_HOST=0.0.0.0（会打印告警）。
API_HOST="${LQ_API_HOST:-127.0.0.1}"
PYTHON_MIN="3.12"

RUN_DIR=".run"
LOG_DIR="data/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR" data/parquet data/duckdb data/cache

C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'; C_DIM='\033[2m'; C_OFF='\033[0m'
info()  { echo -e "${C_GREEN}==> ${C_OFF}$*"; }
warn()  { echo -e "${C_YELLOW}==> ${C_OFF}$*"; }
fail()  { echo -e "${C_RED}==> ${C_OFF}$*"; exit 1; }
dim()   { echo -e "${C_DIM}    $*${C_OFF}"; }

# ----------------------------- 工具函数 ------------------------------------
py()    { .venv/bin/python "$@"; }
lq()    { .venv/bin/lq "$@"; }
pid_ok(){ [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

http_ok() {  # curl 存在才探测；--noproxy 防系统代理劫持 localhost
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf --noproxy '*' -o /dev/null -m 3 "http://localhost:${API_PORT}/api/health/ping"
}

# ---- 变更检测：输入文件的 shasum 未变则跳过该步骤 -------------------------
# usage: if changed_since ".run/pydeps.sha" pyproject.toml; then ...; fi
# 输入文件列表与 stamp 里记录的一致才算「未变」
changed_since() {
  local stamp="$1"; shift
  local current
  current="$(shasum -a 256 "$@" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)" || return 0
  [ -f "$stamp" ] || return 0
  [ "$(cat "$stamp" 2>/dev/null)" = "$current" ] || return 0
  return 1   # 1 = 未变，可跳过
}
mark_done() {  # mark_done <stamp> <files...> —— 成功后写入 stamp
  local stamp="$1"; shift
  shasum -a 256 "$@" 2>/dev/null | shasum -a 256 | cut -d' ' -f1 > "$stamp"
}

# 前端源码指纹：src + 配置 + package.json，源码没变就不重复 next build
web_build_inputs() {
  find web/src web/public -type f 2>/dev/null | sort | xargs shasum -a 256 2>/dev/null
  shasum -a 256 web/package.json web/package-lock.json web/next.config.mjs \
    web/tailwind.config.ts web/tsconfig.json web/postcss.config.mjs 2>/dev/null
}

# 选一个 >= ${PYTHON_MIN} 的 python
pick_python() {
  for cand in python3.13 python3.12 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    local v; v="$("$cand" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)" || continue
    if [ "$(printf '%s\n%s\n' "$v" "$PYTHON_MIN" | sort -V | head -1)" = "$PYTHON_MIN" ]; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

ensure_venv_python() {
  if [ -x .venv/bin/python ]; then
    dim "复用已有 .venv ($(py -V 2>&1))"
    return 0
  fi
  local pypick; pypick="$(pick_python)" || fail "未找到 Python >= ${PYTHON_MIN}，请安装: brew install python@3.12"
  info "创建虚拟环境 ($pypick)"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$("$pypick" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" || true
  fi
  [ -x .venv/bin/python ] || "$pypick" -m venv .venv
  [ -x .venv/bin/python ] || fail "虚拟环境创建失败"
}

install_python_deps() {
  # pyproject.toml（及 uv.lock）没变就跳过重装
  local inputs=(pyproject.toml); [ -f uv.lock ] && inputs+=(uv.lock)
  if ! changed_since "$RUN_DIR/pydeps.sha" "${inputs[@]}"; then
    dim "Python 依赖未变化（pyproject.toml），跳过重装"
    return 0
  fi
  info "安装 Python 依赖（镜像: ${PYPI_INDEX}）"
  if command -v uv >/dev/null 2>&1; then
    UV_DEFAULT_INDEX="$PYPI_INDEX" UV_HTTP_TIMEOUT=120 \
      uv pip install --python .venv/bin/python -e ".[sources,factors,ml,server,dev]" \
      && mark_done "$RUN_DIR/pydeps.sha" "${inputs[@]}"
  else
    .venv/bin/python -m pip install -q --upgrade pip \
      && .venv/bin/python -m pip install -q -i "$PYPI_INDEX" -e ".[sources,factors,ml,server,dev]" \
      && mark_done "$RUN_DIR/pydeps.sha" "${inputs[@]}"
  fi
}

install_node_deps() {
  command -v npm >/dev/null 2>&1 || { warn "未检测到 npm，跳过前端（brew install node）"; return 1; }
  # package.json / lock 未变且 node_modules 存在就跳过
  local inputs=(web/package.json)
  [ -f web/package-lock.json ] && inputs+=(web/package-lock.json)
  if [ -d web/node_modules ] && ! changed_since "$RUN_DIR/npmdeps.sha" "${inputs[@]}"; then
    dim "前端依赖未变化（package.json），跳过 npm install"
    return 0
  fi
  info "安装前端依赖（registry: ${NPM_REGISTRY}）"
  ( cd web && npm install --registry="$NPM_REGISTRY" --no-fund --no-audit ) \
    && mark_done "$RUN_DIR/npmdeps.sha" "${inputs[@]}"
}

ensure_rust() {  # 装好 cargo 返回 0；失败返回 1（调用方降级）
  if command -v cargo >/dev/null 2>&1; then
    dim "cargo 已就绪 ($(cargo --version 2>/dev/null))"
    return 0
  fi
  # 已有 rustup toolchain（rustup-init 装到一半中断很常见）→ 直接挂 PATH 复用
  local tcbin
  for tcbin in "$HOME"/.rustup/toolchains/*/bin; do
    [ -x "$tcbin/cargo" ] || continue
    export PATH="$tcbin:$PATH"
    dim "复用已有 Rust 工具链 ($(cargo --version 2>/dev/null || echo '?'))"
    return 0
  done
  info "安装 Rust 工具链（rustup，失败自动换源，日志: $LOG_DIR/rustup-install.log）"
  command -v curl >/dev/null 2>&1 || { warn "无 curl，无法自动安装 rustup"; return 1; }
  local dist
  for dist in "$RUSTUP_DIST_SERVER" "https://static.rust-lang.org"; do
    if RUSTUP_DIST_SERVER="$dist" RUSTUP_UPDATE_ROOT="$dist/rustup/dist" \
      curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable >>"$LOG_DIR/rustup-install.log" 2>&1; then
      break
    fi
    warn "rustup 源不可用（$dist），换源重试"
  done
  # rustup 默认装在 ~/.cargo，当前 shell 需要 source env 才能找到
  if [ -s "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi
  export PATH="$HOME/.cargo/bin:$PATH"
  if command -v cargo >/dev/null 2>&1; then
    dim "rustup 就绪 ($(cargo --version 2>/dev/null))"
    return 0
  fi
  warn "rustup 安装失败，跳过 Rust 扩展（自动降级 Python 参考实现，功能不受影响）"
  return 1
}

start_redis() {
  REDIS_MODE="none"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose up -d redis >/dev/null 2>&1 && REDIS_MODE="docker" || REDIS_MODE="none"
  elif command -v redis-server >/dev/null 2>&1; then
    if ! .venv/bin/python -c "
import socket,sys
s=socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(('127.0.0.1',6379))==0 else 1)" 2>/dev/null; then
      redis-server --daemonize yes --port 6379 2>/dev/null || true
    fi
    REDIS_MODE="local"
  fi
  case "$REDIS_MODE" in
    docker|local) dim "Redis: $REDIS_MODE" ;;
    *) warn "无 Redis —— 任务队列降级为本地线程（不影响启动，brew install redis 可补）" ;;
  esac
}

wait_api() {
  info "等待 API 就绪 (port $API_PORT) ..."
  for _ in $(seq 1 60); do
    http_ok && { dim "API ready"; return 0; }
    sleep 1
  done
  warn "API 60s 未就绪，看日志: ./lquant.sh logs api"
  return 1
}

spawn() {  # name logfile cmd...
  local name="$1" log="$2"; shift 2
  ( "$@" >>"$log" 2>&1 & echo $! >"$RUN_DIR/$name.pid" )
  disown || true
}

free_port() {  # 端口被残留进程占用时先杀掉，确保能绑定
  local port="$1"
  local pids
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] || return 0
  warn "端口 $port 被占用 (pid: $(echo "$pids" | tr '\n' ' '))，先杀掉"
  echo "$pids" | xargs kill 2>/dev/null || true
  for _ in $(seq 1 10); do
    pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$pids" ] && break
    sleep 0.5
  done
  # 仍不肯走就强杀
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null || true
}

# ============================== 子命令 =====================================
cmd_doctor() {
  echo "== lquant 环境体检 =="
  local p v
  for p in python3 uv npm node redis-server docker cargo curl; do
    if command -v "$p" >/dev/null 2>&1; then
      v="$($p --version 2>/dev/null | head -1 || echo '?')"
      echo "  [✓] $p  $v"
    else
      echo "  [ ] $p  (未安装)"
    fi
  done
  [ -x .venv/bin/python ] && echo "  [✓] .venv  $(py -V 2>&1)" || echo "  [ ] .venv 未创建"
  [ -f data/duckdb/lquant.duckdb ] && echo "  [✓] DuckDB 已建库" || echo "  [ ] DuckDB 未建库 (make db-init)"
  if [ -x .venv/bin/python ]; then
    py - <<'EOF' 2>/dev/null || true
import importlib
mods = {"polars":"核心","duckdb":"存储","baostock":"主数据源","fastapi":"API","rq":"队列"}
missing = [n for n in mods if importlib.util.find_spec(n) is None]
print("  [✓] 关键包齐了" if not missing else f"  [ ] 缺包: {missing}（跑 ./lquant.sh install）")
EOF
  fi
}

cmd_install() {
  info "lquant 依赖安装"

  # 1) uv（没有就装；装不上走 pip 降级）
  if ! command -v uv >/dev/null 2>&1; then
    info "安装 uv 包管理器"
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
      [ -s "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env" || true
      command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
    fi
    command -v uv >/dev/null 2>&1 \
      && dim "uv 就绪" \
      || warn "uv 安装失败，降级用 python3 -m venv + pip（功能不受影响）"
  fi

  # 2) venv + Python 依赖
  ensure_venv_python
  install_python_deps

  # 3) .env
  [ -f .env ] || { cp .env.example .env 2>/dev/null && dim "已生成 .env" || true; }

  # 3.5) git 钩子（相对路径：主仓库与各 worktree 各用各的 scripts/githooks）
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git config core.hooksPath scripts/githooks && dim "git 钩子已就绪 (core.hooksPath=scripts/githooks)"
  fi

  # 4) Rust 扩展（可选，无 cargo 自动装 rustup）
  if ensure_rust; then
    info "编译 Rust 扩展（可选加速）"
    bash scripts/build_rust.sh || warn "Rust 编译失败，自动降级 Python 参考实现"
  fi

  # 5) 前端
  install_node_deps || true

  # 6) 建库（已建库则跳过，删 data/duckdb/lquant.duckdb 可重建）
  if [ -f data/duckdb/lquant.duckdb ]; then
    dim "DuckDB 已建库，跳过 init_db"
  else
    info "初始化 DuckDB"
    py scripts/init_db.py
  fi

  # 7) 冒烟
  py -c "import lquant._rust.loader as l; print('ABI:', l.status())" 2>/dev/null || true

  # 8) git hooks（统一仓库级 hooks，随仓库分发）
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git config core.hooksPath scripts/githooks && dim "git hooks 已启用 (make hooks 可重开)"
  fi

  info "安装完成。下一步: ./lquant.sh bootstrap && ./lquant.sh start"
}

cmd_update() {
  local full=0 no_restart=0
  for a in "$@"; do case "$a" in --full) full=1 ;; --no-restart) no_restart=1 ;; esac; done
  info "同步更新前后端代码"

  # 0) git 仓库且有远程时先拉最新代码
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -n "$(git remote 2>/dev/null | head -1)" ]; then
      info "拉取最新代码 (git pull)"
      git pull --ff-only || warn "git pull 失败（本地改动冲突？），继续基于当前代码更新"
    else
      dim "git 仓库无远程，跳过代码拉取"
    fi
  fi

  # 1) 后端：editable 重装 —— 代码即装即用，同时把 pyproject 依赖变化补齐
  [ -x .venv/bin/python ] || fail "先跑 ./lquant.sh install"
  info "后端依赖同步 (editable 重装)"
  if command -v uv >/dev/null 2>&1; then
    UV_DEFAULT_INDEX="$PYPI_INDEX" UV_HTTP_TIMEOUT=120 \
      uv pip install --python .venv/bin/python -e ".[sources,factors,ml,server,dev]"
  else
    .venv/bin/python -m pip install -q -i "$PYPI_INDEX" -e ".[sources,factors,ml,server,dev]"
  fi

  # 2) 前端：npm install 幂等增量同步 package.json 依赖
  install_node_deps || warn "前端依赖更新失败，前端将以旧依赖继续运行"

  # 3) 前端生产构建：已有生产构建或 --full 才重建（dev 模式无需构建）；源码没变则跳过
  if [ "$full" = 1 ] || { [ -d web/.next ] && [ -f web/.next/BUILD_ID ]; }; then
    if command -v npm >/dev/null 2>&1 && [ -d web/node_modules ]; then
      if [ "$full" = 0 ] && ! changed_since "$RUN_DIR/webbuild.sha" <(web_build_inputs); then
        dim "前端源码未变化，跳过 next build"
      else
        info "前端生产构建 (next build)"
        if ( cd web && npx next build ); then
          web_build_inputs | shasum -a 256 | cut -d' ' -f1 > "$RUN_DIR/webbuild.sha"
        else
          warn "next build 失败，Web 将以旧构建或 dev 模式运行"
        fi
      fi
    else
      warn "npm/node_modules 缺失，跳过前端构建"
    fi
  else
    dim "无生产构建，跳过 next build（Web 走 dev 模式，跑 --full 可强制构建）"
  fi

  # 4) 运行中的服务是旧代码，重启加载
  if [ "$no_restart" = 0 ]; then
    if pid_ok "$RUN_DIR/api.pid" || pid_ok "$RUN_DIR/web.pid" || pid_ok "$RUN_DIR/worker.pid"; then
      info "检测到服务运行中，重启以加载新代码"
      cmd_restart
    else
      dim "服务未运行，跳过重启（./lquant.sh start 启动）"
    fi
  fi
  info "更新完成"
}

cmd_build() {
  info "打包生产构建"
  [ -x .venv/bin/python ] || fail "先跑 ./lquant.sh install"
  [ -d web/node_modules ] || install_node_deps || fail "npm 不可用"

  info "前端生产构建 (next build, standalone)"
  if ! changed_since "$RUN_DIR/webbuild.sha" <(web_build_inputs); then
    dim "前端源码未变化，跳过 next build（删 .run/webbuild.sha 或 ./lquant.sh update --full 可强制重建）"
  else
    ( cd web && npx next build )
    web_build_inputs | shasum -a 256 | cut -d' ' -f1 > "$RUN_DIR/webbuild.sha"
  fi

  info "后端 wheel"
  if command -v uv >/dev/null 2>&1; then
    UV_HTTP_TIMEOUT=120 uv build --wheel --out-dir dist .
  else
    .venv/bin/python -m pip wheel . -w dist --no-deps -q 2>/dev/null \
      || .venv/bin/python -m pip wheel . -w dist --no-deps
  fi

  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  local bundle="dist/lquant-bundle-$ts"
  mkdir -p "$bundle"
  cp dist/*.whl "$bundle/" 2>/dev/null || true
  # standalone 输出自带 server.js + 精简 node_modules
  if [ -d web/.next/standalone ]; then
    mkdir -p "$bundle/web"
    cp -R web/.next/standalone "$bundle/web/"
    mkdir -p "$bundle/web/.next/static" "$bundle/web/public"
    cp -R web/.next/static "$bundle/web/.next/" 2>/dev/null || true
    cp -R web/public "$bundle/web/" 2>/dev/null || true
  fi
  cp -R config scripts "$bundle/" 2>/dev/null || true
  cp -R src/lquant "$bundle/src_lquant" 2>/dev/null || true
  tar -czf "lquant-bundle-$ts.tar.gz" -C dist "lquant-bundle-$ts"
  info "打包完成: lquant-bundle-$ts.tar.gz ($(du -h "lquant-bundle-$ts.tar.gz" | cut -f1))"
}

cmd_bootstrap() {
  [ -x .venv/bin/lq ] || fail "先跑 ./lquant.sh install"
  info "拉取地基数据（日历 + 标的清单）"
  lq data reference --skip-details || true
  info "拉取 ETF 元数据（含 T+N 推断）"
  lq data etf || true
  info "日线回填（哨兵池，断点续传）$*"
  lq data sync "$@" || true
  info "补上市/退市日期（慢，可随时中断续传）—— 前 300 只"
  lq data reference || true
  # 真实源全挂（公司网络/代理常拦 BaoStock 的 TCP）→ 演示数据兜底
  local daily_n; daily_n="$(lq data status 2>/dev/null | grep 'daily parquet' | grep -o '[0-9]*' | tail -1)"
  if [ "${daily_n:-0}" = "0" ]; then
    warn "真实数据源不可达，写入演示数据兜底（换真数据重跑本命令即可覆盖）"
    lq data demo || true
  fi
  lq data status
}

cmd_start() {
  local no_web=0 fg=0
  for a in "$@"; do case "$a" in --no-web) no_web=1 ;; --foreground) fg=1 ;; esac; done

  [ -x .venv/bin/python ] || { warn "依赖未安装，自动执行 install"; cmd_install; }

  # 已在跑就不重复起
  if pid_ok "$RUN_DIR/api.pid"; then warn "API 已在运行 (pid $(cat "$RUN_DIR/api.pid"))"; else
    free_port "$API_PORT"
    start_redis
    info "启动 API (uvicorn, ${API_HOST}:$API_PORT)"
    if [ "$API_HOST" != "127.0.0.1" ] && [ "$API_HOST" != "localhost" ]; then
      warn "API 绑定在 $API_HOST（非回环）。API 无鉴权且会执行用户代码，"
      warn "请确认所在网络可信，否则改回 LQ_API_HOST=127.0.0.1"
    fi
    spawn api "$LOG_DIR/api.log" .venv/bin/uvicorn lquant.server.main:app --host "$API_HOST" --port "$API_PORT"
    wait_api || true
  fi

  # worker：Redis 可用才有意义；降级模式下任务已在 API 进程内跑
  if ! pid_ok "$RUN_DIR/worker.pid"; then
    if .venv/bin/python -c "
import sys
sys.path.insert(0, 'src')
from lquant.server.jobs import _redis_available
sys.exit(0 if _redis_available() else 1)" 2>/dev/null; then
      info "启动 RQ Worker"
      spawn worker "$LOG_DIR/worker.log" .venv/bin/rq worker \
        lquant-default lquant-ingest lquant-backtest \
        --url "${LQ_REDIS_URL:-redis://localhost:6379/0}"
    else
      warn "无 Redis，跳过独立 Worker（任务在 API 进程内本地执行）"
    fi
  fi

  # 监控 worker 进程组（lq worker：1 通用 + K 回测，K=LQ_BACKTEST_WORKERS 或默认 2）
  if ! pid_ok "$RUN_DIR/lqworker.pid"; then
    mkdir -p logs
    if command -v redis-cli >/dev/null 2>&1 && redis-cli -u "${LQ_REDIS_URL:-redis://localhost:6379/0}" ping >/dev/null 2>&1; then
      info "启动监控 Worker 进程组 (lq worker)"
      spawn lqworker logs/worker.log .venv/bin/lq worker
    else
      warn "Redis 不可用，跳过监控 Worker 启动（监控页将显示 offline）"
    fi
  fi

  if [ "$no_web" = 0 ]; then
    if pid_ok "$RUN_DIR/web.pid"; then warn "Web 已在运行 (pid $(cat "$RUN_DIR/web.pid"))"; else
      free_port "$WEB_PORT"
      if [ -d web/.next ] && [ -f web/.next/BUILD_ID ]; then
        info "启动 Web（生产模式 next start, port ${WEB_PORT}）"
        spawn web "$LOG_DIR/web.log" bash -c "cd web && npx next start -p $WEB_PORT"
      else
        warn "无生产构建，用 dev 模式（跑 ./lquant.sh build 可切生产模式）"
        spawn web "$LOG_DIR/web.log" bash -c "cd web && npx next dev -p $WEB_PORT"
      fi
    fi
  fi

  sleep 2
  echo ""
  echo "========================================================="
  echo -e "  ${C_GREEN}lquant 已启动${C_OFF}"
  echo "  API    http://localhost:$API_PORT/docs      日志: ./lquant.sh logs api"
  [ "$no_web" = 0 ] && echo "  Web    http://localhost:$WEB_PORT           日志: ./lquant.sh logs web"
  echo "  状态   ./lquant.sh status    停止   ./lquant.sh stop"
  echo "========================================================="
  command -v open >/dev/null 2>&1 && open "http://localhost:$WEB_PORT" 2>/dev/null || true
  [ "$fg" = 1 ] && { info "前台模式，Ctrl-C 退出（进程继续后台跑）"; tail -f "$LOG_DIR/api.log" "$LOG_DIR/web.log" 2>/dev/null; }
  return 0
}

cmd_stop() {
  info "停止服务"
  for name in web lqworker worker api; do
    if pid_ok "$RUN_DIR/$name.pid"; then
      local pid; pid="$(cat "$RUN_DIR/$name.pid")"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -9 "$pid" 2>/dev/null || true
      dim "$name 已停止 (pid $pid)"
    fi
    rm -f "$RUN_DIR/$name.pid"
  done
  # uvicorn/rq/next 可能有残留子进程
  pkill -f "uvicorn lquant.server.main" 2>/dev/null && dim "清理残留 uvicorn" || true
  pkill -f "rq worker lquant" 2>/dev/null || true
  pkill -f "lq worker" 2>/dev/null || true
  info "done"
}

cmd_status() {
  echo "== lquant 服务状态 =="
  local name pid
  for name in api lqworker worker web; do
    if pid_ok "$RUN_DIR/$name.pid"; then
      echo "  [运行中] $name  pid=$(cat "$RUN_DIR/$name.pid")"
    else
      echo "  [停止]   $name"
    fi
  done
  if http_ok; then
    echo "  [✓] API 健康检查通过 http://localhost:$API_PORT/api/health"
    curl -sf --noproxy '*' -m 3 "http://localhost:$API_PORT/api/health" 2>/dev/null | head -c 400; echo
  else
    echo "  [ ] API 无响应"
  fi
}

cmd_logs() {
  local name="${1:-api}"
  case "$name" in
    api|worker|web) tail -n 100 -f "$LOG_DIR/$name.log" ;;
    lqworker) tail -n 100 -f logs/worker.log ;;
    *) fail "用法: ./lquant.sh logs [api|worker|web]" ;;
  esac
}

cmd_restart() { cmd_stop; sleep 1; cmd_start "$@"; }

case "${1:-help}" in
  install)   cmd_install ;;
  update)    shift; cmd_update "$@" ;;
  build)     cmd_build ;;
  start)     shift; cmd_start "$@" ;;
  all)       shift; cmd_install; cmd_build; cmd_start "$@" ;;
  stop)      cmd_stop ;;
  restart)   shift; cmd_restart "$@" ;;
  status)    cmd_status ;;
  logs)      shift; cmd_logs "$@" ;;
  bootstrap) shift; cmd_bootstrap "$@" ;;
  doctor)    cmd_doctor ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
