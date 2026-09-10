# 回测工作台重构 —— 聚宽式一体化页面（2026-09-11）

## 背景与目标

现状：`/strategies/editor` 与 `/backtests` 两个平级页面，编辑→运行要跨页跳转，「从策略库运行」再跳回编辑器，交互割裂。

目标：仿聚宽把回测做成一体化工作台。`/backtests` 重构为「回测工作台」，策略编辑成为回测的子模块：**编辑、保存、编译运行、结果内联展示一站完成，不跳转**。旧功能全部保留、重新组织。

## 页面结构

`/backtests` 页面顶部一级 Tab：

| Tab | 内容 | 来源 |
|---|---|---|
| 策略回测（默认） | 聚宽式三栏工作台 | 新建 |
| 快速回测 | 因子轮动表单（formula/topN/rebalance） | 现有逻辑原样迁入 |
| 历史与对比 | 回测记录表 + 多运行对比 | 现有逻辑原样迁入 |

三栏工作台：

```
┌──────────────────────────────────────────────┐
│ RunBar：[新建] [保存] [校验] [编译运行 ▶] ●未保存 │
├─────────┬────────────────────────┬───────────┤
│ 策略库  │  Python 编辑器         │ 运行结果  │
│ (用户)  │  (CodeMirror)          │ 指标卡    │
│ 载入/删除│ ── 回测参数 ──        │ 净值图    │
│         │  起止日期 · 因子公式   │ 日志折叠  │
└─────────┴────────────────────────┴───────────┘
```

## 组件划分（web/src/app/backtests/workspace/）

- `page.tsx`：持有工作台状态（selectedId / name / description / code / params / dirty / busy），下传 Pane；顶部一级 Tab 切换三区。
- `RunBar.tsx`：新建 / 保存 / 校验 / 编译运行▶ + 未保存标记●。
- `StrategyPane.tsx`：策略库（用户策略列表，载入、删除——hover 显示删除按钮 + 确认；新建入口）。
- `EditorPane.tsx`：CodeMirror 编辑器（动态加载 ssr:false）+ 参数区（名称/描述/起止日期/因子公式）。
- `ResultPane.tsx`：独立轻量结果组件（不抽详情页代码）：指标卡精简版（收益/年化/夏普/回撤/胜率/费用）+ 净值曲线（策略+基准+超额）+ logs 折叠 + 「查看完整详情」链接。
- `state.ts`:纯函数（dirty 判定、run-code payload 构建、参数解析），vitest 单测。
- `QuickRunPanel.tsx` / `HistoryPanel.tsx`：现有快速回测表单与记录表/对比逻辑**原样迁入**，逻辑零改动。

## 数据流（无后端改动）

- 编译运行：`POST /backtests/run-code`（同步返回 run_id）→ `GET /backtests/{run_id}` → ResultPane 渲染。
- 保存：沿用 POST/PUT `/strategies` 契约（PUT 不可改名；POST source 字段即代码文本）。
- 校验：`POST /strategies/validate`，错误内联在结果区上方。
- 删除：`DELETE /strategies/{sid}`（后端已存在），确认后刷新列表；若删除的是当前载入策略，工作台切回新建态。
- 不引入全局状态库；`useState` + SWR。

## 路由与导航

- 侧边栏「策略编辑」入口移除，仅剩「回测」；`/strategies/editor` 重定向到 `/backtests`，查询参数透传：`?id=xxx` 自动载入策略、`?run=xxx` 载入该次运行的代码（`GET /backtests/{id}/code`）回填为新建态。
- 历史表行尾加「载入」按钮：优先按 strategy_id 载入策略，无 strategy_id 的老记录只支持「载入代码」。
- `/backtests/[runId]` 详情页保留不动，结果区「查看完整详情」链接过去。

## 错误处理

- run-code 失败（含代码错误）→ 错误内联显示在结果区上方，不跳转、不丢代码。
- 运行中显示 elapsed 计时器；不做超时中断（后端同步契约不动）。
- 删除失败 → 错误提示；删除当前策略时工作台复位。

## 测试

- `state.ts` 纯函数 vitest 单测（dirty / payload / 参数解析）。
- 组件测试：RunBar busy 状态与未保存●、EditorPane 参数解析、ResultPane 有/无数据渲染、Tab 切换、StrategyPane 删除确认、HistoryPanel「载入」行为。
- `npm run typecheck` + `npm run test` 全绿，现有测试零回归。

## 明确不做（YAGNI）

- 不做后端超时/异步任务化（run-code 同步契约保持）。
- 不做全局状态库、URL 多参数状态同步（仅 ?id / ?run 两个入参）。
- 不改详情页。
- 不动 ruff 基线（纯前端改动，无 Python 变更）。
