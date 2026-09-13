# 安全边界

lquant 是**单机单人研究工具**，不是多租户服务。请先把这句话读完再决定怎么部署。

## 一句话风险

API 无鉴权，且以下端点会 **exec 用户提供的 Python 代码**：

| 端点 | 入口 | 说明 |
|---|---|---|
| `POST /api/backtests/run-code` | `backtest/jqapi.py` `JQRunner` | 聚宽方言策略代码 |
| `POST /api/analyses`、`PUT /api/analyses/{id}` | `backtest/analysis.py` | 自定义分析片段（保存前冒烟即执行） |
| `POST /api/strategies`、`PUT /api/strategies/{id}` | `backtest/strategy_store.py` | 策略源码保存前校验 |

另外，当 `agent.provider = claude_code` 时，问 AI 会以
`--dangerously-skip-permissions` 拉起 claude CLI 子进程（可任意读写本机）。

**结论：谁能访问这个 API，谁就能在这台机器上执行代码。**

## 三层收口（都不是容器级隔离）

1. **静态校验** —— `backtest/validation.py` `validate_source`
   import 白名单 + 危险调用黑名单（`__import__`/`eval`/`exec`/`compile`/`open`/`getattr`…）
   + 危险属性黑名单（`__class__`/`__subclasses__`/`__globals__`…）。
   调用点：`/api/backtests/run-code`、`/api/analyses*`、`/api/strategies*`。

2. **执行侧受限内建** —— `backtest/sandbox.py` `safe_builtins()`
   给 `exec` 的命名空间**显式**设 `__builtins__`。
   不设的话 CPython 会自动注入完整内建，第 1 层白名单直接失效
   （`__import__("os")` 不是 `import` 语句，AST 白名单看不见它）。

3. **默认只绑回环** —— `lquant.sh` 的 `LQ_API_HOST`，默认 `127.0.0.1`。
   设成 `0.0.0.0` 会打印告警。

### 三层都挡不住什么

exec 语义下，只要能触达任意对象，对象图遍历逃逸
（`().__class__.__bases__[0].__subclasses__()` 找 `os._wrap_close` 拿 `__globals__`）
在理论上始终可达。第 1、2 层是**抬高门槛**，不是证明安全。

要真正隔离，需要第四层：把用户代码丢进受限子进程
（`resource.setrlimit` 限 CPU/内存/进程数 + 禁网 + 超时强杀）。
本仓库已为 BaoStock 做过同构的 watchdog 子进程强杀（`data/watchdog.py`），可复用。

## 部署建议

- **不要**把 API 暴露到公网或不可信内网。默认配置已经只绑回环。
- 需要远程访问时，用 SSH 端口转发，**不要**直接 `LQ_API_HOST=0.0.0.0`：
  ```bash
  ssh -L 8000:127.0.0.1:8000 user@host
  ```
- 只用公开数据、不需要落盘/执行命令时，关掉 CLI 全自主权限：
  ```yaml
  # config/app.yaml
  agent:
    provider: claude_code
    skip_permissions: false    # 不传 --dangerously-skip-permissions
  ```
- CORS 只允许 `http://localhost:3000`（见 `server/main.py`）。改动它等于放宽浏览器侧的
  跨站访问面，谨慎。

## 报告问题

这是个人研究项目，没有正式的漏洞响应流程。如果你发现可稳定利用的逃逸链，
请直接开 issue 并附最小复现。
