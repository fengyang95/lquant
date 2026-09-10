import { taskStatusText } from '@/lib/format';

/** 状态徽章配色：ok=绿、partial=橙、failed/interrupted=红、running=蓝、pending=灰 */
const STATUS_BADGE: Record<string, string> = {
  ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  partial: 'bg-amber-50 text-amber-700 border-amber-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  interrupted: 'bg-red-50 text-red-700 border-red-200',
  running: 'bg-blue-50 text-blue-700 border-blue-200',
  pending: 'bg-neutral-100 text-neutral-500 border-neutral-200',
};

export function TaskStatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded-[2px] border px-1.5 py-0.5 text-xs whitespace-nowrap ${
        STATUS_BADGE[status] ?? 'bg-neutral-100 text-neutral-500 border-neutral-200'
      }`}
    >
      {taskStatusText(status)}
      {status === 'running' && <span className="ml-1 animate-pulse">●</span>}
    </span>
  );
}

/** 任务类型文案 */
export function taskKindText(kind: string): string {
  const m: Record<string, string> = {
    full_backfill: '全量回填',
    daily_update: '每日增量',
  };
  return m[kind] ?? kind;
}

/** 耗时：started → finished（running 时到当前），秒级 */
export function taskElapsed(started: string | null, finished: string | null): string {
  if (!started) return '—';
  const t0 = new Date(started).getTime();
  if (Number.isNaN(t0)) return '—';
  const t1 = finished ? new Date(finished).getTime() : Date.now();
  if (Number.isNaN(t1) || t1 < t0) return '—';
  const sec = Math.round((t1 - t0) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`;
}
