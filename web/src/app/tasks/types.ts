/** 任务中心共享类型与展示辅助。 */

/** 任务进度（与后端 progress 注册表 / WS 流协议一致） */
export type TaskProgress = {
  done: number;
  total: number;
  phase: string;
  message?: string | null;
};

/** 统一任务项（后端 /api/tasks 归一结构） */
export type TaskItem = {
  id: string;
  kind: 'data' | 'sync' | 'backtest' | 'factor' | 'qlib';
  name: string;
  status: string;
  state: 'queued' | 'running' | 'finished' | 'failed' | 'canceled';
  created_at: string | null;
  params: Record<string, unknown>;
  error: string | null;
  progress?: TaskProgress | null;
};

/** /api/tasks/summary 的单个 kind 统计 */
export type KindSummary = {
  total: number;
  running: number;
  failed: number;
  succeeded: number;
  canceled: number;
};

export type TaskSummary = {
  kinds: Record<TaskItem['kind'], KindSummary>;
};

export const KIND_TEXT: Record<TaskItem['kind'], string> = {
  data: '数据任务',
  sync: '同步任务',
  backtest: '回测任务',
  factor: '因子挖掘',
  qlib: 'Qlib',
};

/** 统一状态 → 徽章配色（沿用 TaskBadge 语义：running=蓝、failed=红、finished=绿） */
export const STATE_BADGE: Record<TaskItem['state'], string> = {
  queued: 'bg-neutral-100 text-neutral-500 border-neutral-200',
  running: 'bg-blue-50 text-blue-700 border-blue-200',
  finished: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  canceled: 'bg-neutral-100 text-neutral-500 border-neutral-200',
};

export const STATE_TEXT: Record<TaskItem['state'], string> = {
  queued: '排队中',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
  canceled: '已取消',
};

/** 参数摘要：键值对拍平成 "k=v" 串，最多 3 个，其余计入 +N */
export function paramsBrief(params: Record<string, unknown> | null | undefined): string {
  if (!params) return '';
  const entries = Object.entries(params);
  if (!entries.length) return '';
  const head = entries
    .slice(0, 3)
    .map(([k, v]) => `${k}=${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
    .join(' ');
  const rest = entries.length - 3;
  return rest > 0 ? `${head} +${rest}` : head;
}

/** 时间展示：created_at 截到 分（缺省 —） */
export function createdText(t: string | null): string {
  return t ? t.slice(0, 16).replace('T', ' ') : '—';
}
