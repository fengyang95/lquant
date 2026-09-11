# 回测工作台（聚宽式一体化）实施计划

> **For agentic workers:** 本计划采用主会话 Inline 执行（superpowers:executing-plans）。

**Goal:** `/backtests` 重构为聚宽式一体化回测工作台——策略编辑成为回测子模块，编辑/保存/校验/编译运行/内联结果一站完成。

**Spec:** `docs/superpowers/specs/2026-09-11-backtest-workspace-design.md`

**Stack:** Next.js 15 / React 19 / SWR / CodeMirror / Tailwind / vitest + testing-library

## 约束

- 全部改动在 `web/` 内，不改 Python 后端（DELETE /strategies/{sid} 已存在）。
- 设计系统沿用 Panel/Stat/Empty/Loading/ErrorNote + btn/input/table-dense/tag；红涨绿跌。
- API 用 `src/lib/api.ts` 的 get/post/putData/del（裸契约）。
- 测试放同目录 `__tests__/`，jsdom 下 `vi.mock('next/navigation')`。
- 每任务 TDD + 独立 conventional commit；收尾 `npm run typecheck && npm run test` 全绿。

## 任务

1. **state.ts**：`workspace/state.ts` 纯函数——`parseFormulas` / `buildRunPayload`（end 空省略、strategyId null 省略）/ `isDirty`（base null 时全空非 dirty、任一非空即 dirty；非 null 逐字段比较）+ 类型 `EditorParams/StrategyMeta/RunPayload/Snapshot`。TDD。
2. **RunBar**：受控组件 `{dirty, busy: ''|'save'|'validate'|'run', onNew/onSave/onValidate/onRun}`；文案 新建/保存(保存中…)/校验(校验中…)/编译运行 ▶(运行中…)；busy 全禁用；dirty 显示「●未保存」。
3. **StrategyPane**：`{strategies, selectedId, onLoad(id), onDelete(id, name), onNew()}`；选中高亮；hover 删除→确认态（确定/取消，内部 confirmingId local state，不调 API）；空态「策略库还是空的 —— 点「新建」写一个」；底部「+ 新建策略」。
4. **EditorPane**：`{name, description, code, params: EditorParams, selectedId, onChange(next: Snapshot)}` 全量快照 onChange；CodeMirror dynamic ssr:false + python() + oneDark + 460px；名称 selectedId 非空时只读；字段：名称/描述/开始/结束日期/因子公式；不调 API。
5. **ResultPane**：`{runId: string | null}`；runId 非空 SWR `/backtests/{runId}` → 六项指标卡（收益/年化/夏普/回撤/胜率/费用，红涨绿跌）+ 净值图（策略/基准/超额，双轴，option 组件内 useMemo）+ 日志折叠 + 「查看完整详情」Link；runId null → Empty「点「编译运行 ▶」开始第一次回测」。测试 mock `@/components/Chart` 与 `swr`。
6. **QuickRunPanel / HistoryPanel**：旧 page「运行回测」表单与「回测记录+对比」原样迁入，HistoryPanel 自持 SWR(/backtests, 5s)+picked/cmp+compare，新增 `onLoadRun(row)` prop + 行尾「载入」按钮。
7. **page.tsx 编排**：三 Tab（策略回测/快速回测/历史与对比）；持有 selectedId/name/description/code/params/base(快照)/busy/notice/errors/runId；loadStrategy(id) 与 loadRunCode(runId) 共享 helper（?id/?run 参数与历史「载入」走同一路径）；编译运行→run-code→setRunId（错误内联不丢码）；保存 POST/PUT 契约（loadedConfig 合并 factor_formulas）；默认导出包 Suspense（useSearchParams）；?id→loadStrategy、?run→loadRunCode（回填新建态不自动运行）；删除当前策略→复位新建态；Sidebar 移除「策略编辑」；`/strategies/editor` 整页替换为 `redirect('/backtests')`；Sidebar.test 补断言。
8. **收尾**：typecheck + 全量测试全绿；手动冒烟交用户验收。

## 明确不做

- elapsed 计时器不做（运行中显示「运行中…」）——需同步修订 spec 错误处理节该句。
- 不做后端异步任务化、全局状态库；不改详情页；不动 ruff。
