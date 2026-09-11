# 仓库代码 Review（2026-09-11）

> 范围：`src/lquant`（230 files / 25.8k 行）、`tests`（106 files / 13.7k 行）、`crates`、`web`、`config`、CI 与工程元信息。
> 方法：全文走读 + 静态扫描 + 实跑验证。下面的每条都带可复现证据。

## 0. 现状体检（先给结论）

| 项 | 结果 | 说明 |
|---|---|---|
| 单元测试 | **732 passed / 9 skipped / 0 failed** | `TMPDIR` 指向工作区内目录后实跑。原本看到的 252 个 ERROR 是执行环境沙箱拦截 pytest 建 tmp 目录所致，非仓库问题 |
| `ruff check` | ✅ 全绿（0 违规） | `pyproject.toml` 的 ignore 列表已把历史债收口 |
| `ruff format --check` | ❌ **302 / 337 文件需重排** | 说明仓库基本没跑过 `ruff format`，风格靠人肉维持 |
| `mypy` | ⚠️ 未通过验证 | `make type` 存在，但本地跑不到结束（被超时 kill），CI 也没有这一步 |
| CI | ⚠️ 只跑 `cargo test` / `pytest` / web 三件套 | **lint、类型、format 全缺**，债务必然回潮 |
| Rust 扩展 | `python-fallback` | 未 `maturin develop`，降级路径正常工作（符合设计） |

测试覆盖本身是很扎实的：714 个用例、六层回测验证（L1–L4）、因子 F1–F6 金标准 + 恒等式断言，这套「文档 = 测试 = 脚本」的三层同源做得很罕见，是仓库最大的亮点。

---

## P0 — 安全（建议优先处理）

### P0-1 用户代码 exec「沙箱」可被两步绕过 → 配合 `--host 0.0.0.0` 等于局域网 RCE

**证据链：**

1. `src/lquant/backtest/validation.py:20-33` 的 `validate_source` **只检查 `ast.Import` / `ast.ImportFrom` 节点**，不看调用表达式。
2. `src/lquant/backtest/analysis.py:51` 与 `src/lquant/backtest/jqapi.py:327` 直接 `exec(source, ns)`；`_ns()`（analysis.py:20-26）**没有设 `__builtins__`**，Python 会自动注入完整 builtins。
3. 于是 `__import__("os")` 完全绕过白名单。实测：

```
静态校验结果(应为空=通过): []
exec 实际执行了 __import__("os") 并拿到 cwd => ['/Users/lyp/WorkBuddy/.../main-d8e5d434']
```

（`tests/unit/test_analysis_sandbox.py::test_bad_import_rejected` 只覆盖了 `import os` 这种「老实写法」，所以测试是绿的，但防线是纸糊的。）

4. `lquant.sh:436` 启动 API 用的是 **`--host 0.0.0.0`**，而 `jqapi.py:32-33` 的 docstring 却写着「API 只绑 127.0.0.1/内网，不暴露公网」——**设计与实现相互矛盾**。整个 API 没有任何鉴权。
5. 叠加 `src/lquant/agent/claude_code.py:106` 的 **`--dangerously-skip-permissions`**（子进程可以任意读文件、跑命令），以及 `run_user_analysis` 是无鉴权的 HTTP 端点。

**后果**：同一局域网内任何人都可以 POST 一段代码拿到 shell。

**建议（按性价比排序）：**

- 【必做】`lquant.sh` / `Makefile` 默认绑 `127.0.0.1`，需要对外时用 `LQ_API_HOST=0.0.0.0` 显式打开，并在启动日志里大字告警。
- 【必做】`exec` 前替换命名空间 builtins：`ns["__builtins__"] = {k: builtins.__dict__[k] for k in _SAFE_BUILTINS}`，只留 `len/range/zip/enumerate/sum/min/max/abs/round/sorted/dict/list/set/tuple/str/int/float/bool/print` 等，去掉 `__import__/open/eval/exec/compile/getattr/setattr/globals/locals/input/os/sys`。
- 【强烈建议】把用户代码执行挪到**受限子进程**（`resource.setrlimit` 限内存/CPU + `RLIMIT_NPROC` + 无网络命名空间），超时强杀。仓库已经为 BaoStock 做过 watchdog 子进程强杀（`data/watchdog.py`），同一套思路可以复用。
- 【建议】`--dangerously-skip-permissions` 改成配置项 + 启动告警，不要硬编码。
- 【建议】`validate_source` 增加 `ast.Call` 检查：禁止 `__import__`/`eval`/`exec`/`compile`/`open`/`globals`/`locals`/`getattr`（这层能挡脚本小子，挡不住有心人，所以不能替代前两条）。

---

## P1 — 正确性与健壮性

### P1-1 配额「硬护栏」是读-改-写的非原子更新，会少记

`src/lquant/factors/agents.py:64-80`：

```python
con.execute("INSERT OR REPLACE INTO agent_ledger VALUES (?,?,?,?)",
            [agent, eval_usage(agent) + n, ...])
```

两个问题：
- **lost update**：先算 `eval_usage()` 再写回，并发两次调用会各自读到同一个旧值，只累加一次。
- **同事务里开第二条连接**：`eval_usage` 走 `reader()`，而外层 `writer()` 事务还开着——正好踩中 `core/db.py` docstring 警告的「读写实例不一致」风险。

**修法**：改成 `INSERT INTO agent_ledger(agent, eval_count, ...) VALUES (?, ?, ...) ON CONFLICT(agent) DO UPDATE SET eval_count = agent_ledger.eval_count + excluded.eval_count, updated_at = excluded.updated_at`。

### P1-2 API 挖掘路径绕过累计配额，与 CLI 路径口径不一

- `src/lquant/server/api/factors.py:472-482` 的 `_validate_mine_req` 只比 `n > a.quota_eval`（**静态配置值**），从不查 `eval_usage`，跑完也**不调 `record_eval`**。
- 真正记账的只有 CLI：`src/lquant/cli/commands/factor.py:87-99`（这里才是「剩余配额」+ 校正门槛 `corrected_threshold` 的完整护栏）。

**后果**：`/api/factors/mine` 可以无限次调用、每次都按满配额跑，账本永远是空的。方案里写的「同一门禁、同一口径、同一份记账」在 API 路径上没有落地。

### P1-3 「单写者」约束只靠约定，没有任何强制

`src/lquant/core/db.py:22-46` 的 docstring 写「同一时刻只允许一个进程持有」，但：

- `writer()` 与 `reader()` **完全同构**，`reader()` 拿到的也是读写连接；
- 没有任何进程锁 / 文件锁 / 心跳文件。

叠加 `--host 0.0.0.0` + RQ worker 多进程 + uvicorn `--reload`（dev.sh 里同时起 API 和 worker），多个进程同时在写 DuckDB 只会撞运行时锁并报错，而不是被提前拦住。

**建议**：加一个 `.duckdb.lock`（`fcntl.flock` / `msvcrt`）在 `writer()` 进入时非阻塞抢占，抢不到就抛明确错误；或至少在启动时校验。

### P1-4 问 AI 的发消息接口用「轮询等落库」是脆弱设计

`src/lquant/server/api/ask.py:96-113`：先 `create_task(run())`，再以 40×50ms 轮询 `get_messages()` 找**最后一条 user 消息**，找不到就 500。

- 同一会话并发两次发消息，轮询可能取到**另一条** user 消息；
- 2 秒上限到了就 `svc.cancel(sid)` 把后台任务掐掉，用户消息可能已经落库但被当成失败；
- 本质是「写操作放在后台任务里」导致的时序问题。

**修法**：在端点里**同步 `await svc.store.add_message(sid, "user", content)`**（或让 `send_message` 接受已落库的 user 消息），再起后台任务跑 agent。删掉整个 `_POLL_TIMES` 机制。

### P1-5 任务队列：Redis 模式下协作取消名存实亡，且连接状态被永久缓存

`src/lquant/server/jobs.py`：

- `_redis_available()`（:19-30）和 `get_redis()`（:33-39）都是 `lru_cache(maxsize=1)`。Redis 启动后才通、或中途掉线重连，判定结果永远不会刷新（`get_redis` 还会一直复用可能已断的 client）。
- `enqueue`（:148-203）在 `if _redis_available(): ... return job` **之后**才注入 `cancel_check`，注释却写「RQ 模式同样透传」——实际 Redis 分支从不注入。而且 `cancel_check` 闭包指向进程内 `_CANCELABLE`，RQ worker 是另一个进程，**共享不到**。所以 RQ 下的「协作式取消」是假的。
- `LocalJob._canceled` 后 `_run` 仍会把 `job._result` 写进去，docstring 说的「结果被丢弃」并没有实现（`get_status()` 会返回 canceled，但 `result` 属性仍能读到）。

**建议**：取消语义要么用 Redis 里的标记位（跨进程可读）真正做实，要么把文档改成「仅本地降级模式支持取消」；`_redis_available` 改成带 TTL 的探测（例如 30s 缓存）。

### P1-6 `fetch_quotes` 的 timeout 是死参数

`src/lquant/market/ticks.py:32` 写着 `timeout  # noqa: B018 - 透传给注入后端`，但真实后端 `_realtime_backend`（:59-70）**签名里根本没有 timeout**，也不往下传。docstring 承诺的「整体预算」在真实路径上不成立——一个卡住的 provider 会把线程池里的线程一直占住。

---

## P2 — 一致性与工程化

### P2-1 硬约束 2（ISO 8601 + Asia/Shanghai）没有贯彻

仓库自己定的三条硬约束之一，但实际：

- naive `datetime.now()` / `date.today()`：**79 处**
- aware `now_cn()` / `today_cn()`：**28 处**

危险的典型（不是审计时间戳，而是**业务日期**）：

- `data/ingest/daily.py:208`、`minute.py:33`、`financial.py:28`、`adj.py:29`：`end` 缺省时用 `date.today()` 当回填右边界；
- `market/collectors/dragon_tiger.py:92`：`datetime.now().date() - timedelta(days=1)` 推「昨天」；
- `market/collectors/sector.py:39`、`money_flow.py:75`、`index_daily.py:44`；
- `data/ingest/crosscheck.py:128-129`、`data/fallback.py:111`。

部署在 UTC 容器上时，北京时间 00:00–08:00 之间跑的同步任务会**整体偏一天**。建议全量替换为 `today_cn()`，并加一条 lint 规则（ruff 的 `DTZ` 系列，select 里加 `DTZ`）把 naive 时间用法挡在 CI 外。

### P2-2 `pytest.ini` 与 `pyproject.toml` 双配置，后者被完全忽略

- `pytest.ini` 存在（4 行），`pyproject.toml` 里也有 `[tool.pytest.ini_options]`。
- pytest 的优先级是 **pytest.ini > pyproject.toml**，所以 `pyproject.toml` 里的 `addopts = "-q --strict-markers"` 和 `markers`（slow/rust）**全部失效**。
- 直接后果：实跑日志里出现 `PytestUnknownMarkWarning: Unknown pytest.mark.rust`（`tests/unit/test_rust_alignment.py:118` 等），`--strict-markers` 没生效，marker 拼错也不会报错。

**修法**：删掉 `pytest.ini`，把 `asyncio_mode = auto` 合并进 `pyproject.toml`（或反过来，二选一）。

### P2-3 CI 不跑 lint / 类型 / 格式

`.github/workflows/ci.yml` 的 python job 只有 `pytest`。grep `ruff|mypy` 结果为 0。配合 P0 之外的三点：

- `ruff check` 现在是**全绿**的——正是接入 CI 的最佳时机（不会再被历史债挡住）；
- `ruff format --check` 有 302 个文件待重排，建议单独一个「只动格式」的提交，然后把 `ruff format --check` 加进 CI；
- `make type` 已在 Makefile 里，接进 CI 需要先把 mypy 跑到能收敛（建议先 `--follow-imports=skip` 或限定 `src/lquant/core` 等目录逐步放开）。

顺带：githooks 的 `pre-commit` 只检查 `src/ scripts/`，漏了 `tests/`、`web/`、`crates/`。

### P2-4 工程元信息缺失

- **没有 LICENSE 文件**，但 `pyproject.toml:10` 声明 `license = { text = "MIT" }`。既然走 PR 流程，建议补齐。
- 没有 `CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`。至少 SECURITY.md 在 P0-1 修完前是需要的（说明「仅限本地可信环境使用」）。
- `pyproject.toml` 的 dev 依赖里有 `pre-commit`，但仓库**没有 `.pre-commit-config.yaml`**（改用自研 githooks）→ 该依赖是冗余的，要么删依赖，要么改回标准 pre-commit。

### P2-5 文档与代码漂移

- `docs/FACTOR_VALIDATION.md` 写 N1–N6 状态为「⏳ M2.5 中性化里程碑」，但 `src/lquant/factors/preprocess/neutralize.py` 已实现（9 个函数），且已有 `test_n2_self_neutralize_zero` / `test_n4_residual_orthogonal` / `test_n6_all_covariates_missing_raises`。文档该更新成 ✅。
- `docs/EXTENSION_POINTS.md` 的档位标记（EP-5 M6b / EP-8 M6c 等）同样需要跟代码对一遍。
- `docs/BACKTEST_VALIDATION.md` 的 L6（模拟盘对账）仍是预留项，`docs/BACKTEST_ENGINES.md` 的 B5（Polars 向量化参数扫描）标记为未做——这两个是真实缺口，建议在 README 的「当前能力」里如实标注，避免误用。

### P2-6 若干 API/框架用法已过时或不严谨

- `src/lquant/server/main.py:44-100`：`@app.on_event("startup"/"shutdown")` 已废弃，FastAPI 推荐 `lifespan`；实跑有 DeprecationWarning。而且同一个模块里对「包了 middleware 的 app」和「底层 FastAPI app」分别注册 startup，可读性较差，建议收成一个 `lifespan`。
- `src/lquant/server/ws.py:32, 105`：协程内 `asyncio.get_event_loop()`，应改 `asyncio.get_running_loop()`。
- `src/lquant/server/api/ask_bus.py`：订阅队列是**无界** `asyncio.Queue`，慢/僵尸订阅者会导致内存无界增长；且没有事件回放，WS 断线重连会丢中间事件。建议 `Queue(maxsize=N)` + 溢出策略 + 断线重连时按 `since` 补发。
- `src/lquant/server/envelope.py:88-103`：`_wrap` 用「body 里有没有 `code` 键」判断是否已封套，业务数据恰好含 `code` 字段（比如证券代码 `code`）会被误判成已封套而漏包。建议换成显式标记。

### P2-7 静默吞异常，护栏 fail-open

`except Exception: pass` 出现在配额记账（`factors/agents.py:78`）、ledger 占位（`server/api/factors.py:506`）等**记账/护栏类**逻辑里。这些地方失败应该至少 `_LOG.warning` 并计数，否则配额护栏会在「表不存在」等情况下静默失效，而调用方看到的是「一切正常」。

（`server/api/factors.py:544` 的 `pass  # 记账失败不阻断结果` 这种有注释的是可接受的取舍，值得保留。）

---

## 亮点（别改坏了）

- **测试密度与验证体系**：714 个用例、L1–L4 回测六层验证、F1–F6 因子金标准 + 数学恒等式（完美因子 IC==1、IC 反对称、RankIC 单调不变、截断不变性抓未来函数）。`docs/<X>_VALIDATION.md` 与测试、脚本三者同源，这种纪律非常罕见。
- **降级设计很务实**：无 Redis 降级本地线程、无 cargo 降级纯 Python 参考实现（`_rust/*.py`）、BaoStock 静默挂起统一走子进程 watchdog 强杀。README 里「一个人扛不住编译失败全线停摆」的取舍讲得很清楚。
- **三条硬约束有可执行落点**：`core/types.py` 的 `Symbol` 强制带交易所后缀、`parse_symbol` 归一 4 种输入格式；金额换算收口在 adapter 层。
- **单写者 DB 的 docstring** 把 `read_only=True` 会导致实例不一致的坑写得很明白——这种「写清楚为什么」的注释比代码本身值钱。

---

## 建议的行动顺序

1. **P0-1**（绑定地址 + builtins 收口）—— 半小时内就能把最大的坑堵上。
2. **P2-2**（删 `pytest.ini` 或合并）—— 5 分钟，顺手修掉 marker 告警。
3. **P1-1 / P1-2**（配额记账原子化 + API 路径接同一口径）—— 属于「方案承诺了但没落地」，优先补齐。
4. **P2-3**（CI 接 `ruff check` + `ruff format --check`）—— 现在 lint 全绿，是唯一低成本窗口。
5. **P1-4 / P1-6**（ask 发消息时序 + timeout 死参数）—— 影响可感知的线上行为。
6. **P2-1**（时区全量收敛 + 加 `DTZ` lint）—— 工作量中等，但直接决定数据正确性。
7. 其余按模块迭代时顺手清。
