.PHONY: help setup hooks db-init db-reset bootstrap dev api worker web \
        test coverage diff-cov lint fmt type rust-build rust-test \
        docker-up docker-down clean smoke start stop status logs bundle

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

setup:
	bash lquant.sh install

hooks:
	git config core.hooksPath scripts/githooks
	@echo "git hooks 已启用 (pre-commit / pre-push / post-rewrite / post-merge)"

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
