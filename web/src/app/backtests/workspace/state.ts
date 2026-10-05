/** 回测工作台纯逻辑 —— dirty 判定 / run-code payload 构建 / 参数解析。与 React 解耦便于单测。 */

export type EditorParams = { start: string; end: string; formulas: string };

export type StrategyMeta = {
  id?: string;
  name: string;
  source: 'builtin' | 'user';
  description?: string;
  version?: number;
  updated_at?: string;
};

export type RunPayload = {
  code: string;
  start: string;
  end?: string;
  factor_formulas: string[];
  strategy_id?: string;
};

export type Snapshot = {
  name: string;
  description: string;
  code: string;
  params: EditorParams;
};

/** 逗号分隔的因子公式串 → trim 后去空的数组。 */
export function parseFormulas(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '');
}

/** 构建 run-code 请求体：end 为空串时省略 end；strategyId 为 null/'' 时省略 strategy_id。 */
export function buildRunPayload(
  code: string,
  params: EditorParams,
  strategyId: string | null,
): RunPayload {
  const payload: RunPayload = {
    code,
    start: params.start,
    factor_formulas: parseFormulas(params.formulas),
  };
  const withEnd = params.end !== '' ? { ...payload, end: params.end } : payload;
  return strategyId ? { ...withEnd, strategy_id: strategyId } : withEnd;
}

/** 编辑快照与基准快照对比：不同则视为 dirty。base 为 null 时，全空视为未修改。 */
export function isDirty(cur: Snapshot, base: Snapshot | null): boolean {
  if (base === null) {
    const allEmpty =
      cur.name === '' &&
      cur.description === '' &&
      cur.code === '' &&
      cur.params.start === '' &&
      cur.params.end === '' &&
      cur.params.formulas === '';
    return !allEmpty;
  }
  return (
    cur.name !== base.name ||
    cur.description !== base.description ||
    cur.code !== base.code ||
    cur.params.start !== base.params.start ||
    cur.params.end !== base.params.end ||
    cur.params.formulas !== base.params.formulas
  );
}

// ---------------------------------------------------------------- 运行中反馈
// 回测实测 26~39s。此前前端只有一句「执行中…」，没有阶段、没有已用时长，
// 也无法取消 —— 用户分不清"在正常跑"和"卡死了"。下面是与 React 解耦的纯逻辑，
// 便于单测（轮询节奏 / 时长格式化 / 进度文案）。

export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'canceled';

export type JobProgress = {
  done?: number;
  total?: number;
  phase?: string;
  message?: string | null;
} | null;

export const RUN_POLL_BASE_MS = 2000;
export const RUN_POLL_MAX_MS = 15000;
/** 单次回测的兜底上限：超时后提示用户去历史列表看，而不是无限转圈。 */
export const RUN_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * 轮询退避：前几秒密一点（快速拿到首个进度），随后逐步拉长到上限。
 * 既让"刚提交"有即时反馈，又不为一个几十秒的任务打上百次请求。
 */
export function pollDelayMs(attempt: number): number {
  if (attempt <= 0) return RUN_POLL_BASE_MS;
  const grown = RUN_POLL_BASE_MS * 1.5 ** attempt;
  return Math.min(RUN_POLL_MAX_MS, Math.round(grown));
}

/** 已用时长文案：<60s 显示秒，否则 mm:ss。 */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * 运行中提示文案：状态 + 阶段 + 已用时长。
 * 有 total 时带上「done/total」，让长任务看起来在推进而不是静止。
 */
export function describeRun(
  status: RunStatus,
  progress: JobProgress,
  elapsedMs: number,
): string {
  const elapsed = formatElapsed(elapsedMs);
  if (status === 'queued') return `已提交，排队中… ${elapsed}`;
  const parts: string[] = [];
  if (progress?.phase) parts.push(progress.phase);
  if (progress?.done != null && progress?.total) {
    parts.push(`${progress.done}/${progress.total}`);
  }
  const head = parts.length ? parts.join(' ') : '执行中';
  return `${head}… ${elapsed}`;
}

/**
 * 有未保存改动时的二次确认。
 *
 * 载入别的策略 / 切 Tab / 新建都会直接丢弃编辑器内容 —— 用户写了半天代码
 * 点一下左栏就没了。dirty 为假时不打扰（与浏览器原生 confirm 语义一致：
 * 返回 true = 继续执行）。
 */
export function confirmDiscard(
  dirty: boolean,
  action: string,
  confirmFn: (msg: string) => boolean,
): boolean {
  if (!dirty) return true;
  return confirmFn(`当前策略有未保存的修改，${action}将丢弃这些修改。继续？`);
}
