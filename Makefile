.PHONY: help setup hooks db-init db-reset bootstrap dev api worker web \
        test coverage diff-cov lint fmt type rust-build rust-test \
        docker-up docker-down clean smoke start stop status logs bundle \
        secrets secrets-history secrets-dir secrets-install public-ready

UV      ?= uv
PY      ?= .venv/bin/python
RUN     ?= .venv/bin/lq

help:
	@echo "lquant 常用命令"
	@echo "  make setup       一键安装全部依赖（等价 ./lquant.sh install）"
	@echo "  make start       一键启动全栈（后台常驻，等价 ./lquant.sh start）"
	@echo "  make db-init     建 DuckDB 表结构"
	@echo "  make db-reset    删库重建（危险）"
	@echo "  make bootstrap   拉取地基+日线数据（哨兵池；full=全市场）"
	@echo "  make dev         开发模式 API + Worker + Web（热重载）"
	@echo "  make api         仅 API (uvicorn --reload)"
	@echo "  make worker      仅 RQ worker"
	@echo "  make web         仅前端"
	@echo "  make bundle      生产构建 + 打发行包（等价 ./lquant.sh build）"
	@echo "  make smoke       ABI 冒烟：确认 Rust 扩展可加载"
	@echo "  make test / lint / fmt / type"
	@echo "  make coverage    全量覆盖率报告（coverage.xml/json）"
	@echo "  make diff-cov    改动行覆盖率门禁（vs origin/main，≥95%）"
	@echo "  make rust-build  编译 Rust 扩展（maturin develop）"
	@echo "  make docker-up   启动 Redis"
	@echo "  make secrets     扫描暂存区密钥（与 pre-commit 同一份规则）"
	@echo "  make secrets-history 全历史密钥审计（所有分支，含 merge diff）"
	@echo "  make secrets-install 安装固定版本 gitleaks（免 sudo，所有 worktree 共享）"
	@echo "  make public-ready    公开前体检：全历史 + 工作区 + 卫生（严格）"

setup:
	bash lquant.sh install

hooks:
	git config core.hooksPath scripts/githooks
	@echo "git hooks 已启用 (pre-commit / pre-push / post-rewrite / post-merge)"

# --- 密钥 / 敏感信息 ---------------------------------------------------------
# 四个目标都只是 scripts/secret_scan.sh 的薄封装：钩子、CI、手工审计共用
# 同一份实现与同一份 .gitleaks.toml，避免"本地过了 CI 挂"。

# 暂存区（pre-commit 跑的就是这个）
secrets:
	bash scripts/secret_scan.sh staged

# 全历史 + 所有分支。公开仓库前必须跑，且必须干净。
secrets-history:
	bash scripts/secret_scan.sh history

# 工作区已跟踪文件快照（覆盖未提交改动）
secrets-dir:
	bash scripts/secret_scan.sh dir

# 装 gitleaks（固定版本，装到 .git 下，所有 worktree 共用）
secrets-install:
	bash scripts/install_gitleaks.sh

# 公开前体检：全历史 + 工作区 + 卫生（本机绝对路径按失败处理）
public-ready:
	bash scripts/secret_scan.sh public

start:
	bash lquant.sh start

stop:
	bash lquant.sh stop

status:
	bash lquant.sh status

logs:
	@bash lquant.sh logs ${target}

bundle:
	bash lquant.sh build

db-init:
	$(PY) scripts/init_db.py

db-reset:
	@echo "将删除 data/duckdb/lquant.duckdb，Ctrl-C 取消"; sleep 3
	rm -f data/duckdb/lquant.duckdb
	$(MAKE) db-init

bootstrap:
	bash lquant.sh bootstrap

bootstrap-full:
	bash lquant.sh bootstrap --full

dev:
	bash scripts/dev.sh

api:
	.venv/bin/uvicorn lquant.server.main:app --reload --port 8000

worker:
	.venv/bin/rq worker lquant-default lquant-ingest --url redis://localhost:6379/0

web:
	cd web && npm run dev

smoke:
	$(PY) -c "import lquant._rust.loader as l; print(l.status())"

rust-build:
	bash scripts/build_rust.sh

rust-test:
	cd crates && cargo test --workspace

test:
	$(PY) -m pytest tests -m "not slow"

# 覆盖率报告：出 term-missing + xml（diff-cover 用）+ json
coverage:
	$(PY) -m pytest tests -m "not slow" \
	  --cov=src/lquant --cov-report=term-missing \
	  --cov-report=xml:coverage.xml --cov-report=json:coverage.json -q

# 增量门禁：本次改动（vs origin/main）的 diff 覆盖率 ≥95%
# 首次先 make coverage 生成 coverage.xml；uv sync 装 diff-cover
diff-cov:
	$(PY) -m diff_cover.diff_cover_tool coverage.xml \
	  --compare-branch=origin/main --fail-under=95

lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff format src tests

type:
	.venv/bin/mypy src/lquant

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
