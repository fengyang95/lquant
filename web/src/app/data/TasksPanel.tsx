'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import Chart from '@/components/Chart';
import { fetcher, post } from '@/lib/api';
import { taskElapsed, TaskStatusBadge, taskKindText } from './TaskBadge';
import BackfillModal from './BackfillModal';
import type { DataTask } from './types';

/** 近 50 条任务按 kind × status 堆叠条形图：一眼看出各类任务的成败分布 */
const STATUS_COLORS: Record<string, string> = {
  ok: '#0f8a5f',
  partial: '#c07f00',
  running: '#3b7dd8',
  pending: '#9ca3af',
  failed: '#c0392b',
  interrupted: '#c0392b',
};

function TasksSummaryChart({ tasks }: { tasks: DataTask[] }) {
  const kinds = [...new Set(tasks.map((t) => t.kind))];
  const statuses = ['ok', 'partial', 'running', 'pending', 'failed', 'interrupted'];
  const count = (kind: string, s: string) =>
    tasks.filter((t) => t.kind === kind && t.status === s).length;
  return (
    <div className="mb-3">
      <Chart
        height={Math.max(100, kinds.length * 42)}
        option={{
          grid: { left: 8, right: 16, top: 4, bottom: 20, containLabel: true },
          tooltip: { trigger: 'axis' as const },
          legend: { bottom: 0, itemWidth: 10, itemHeight: 10 },
          xAxis: { type: 'value' as const, minInterval: 1 },
          yAxis: {
            type: 'category' as const,
            data: kinds.map((k) => taskKindText(k)),
            axisTick: { show: false },
          },
          series: statuses.map((s) => ({
            name: s,
            type: 'bar' as const,
            stack: 'total',
            barWidth: 14,
            data: kinds.map((k) => count(k, s)),
            itemStyle: { color: STATUS_COLORS[s] ?? '#9ca3af' },
          })),
        }}
      />
    </div>
  );
}

/** 进行中（running/pending）任务存在 → 列表 2s 轮询（spec 允许的轮询路径） */
function hasActive(tasks: DataTask[] | undefined): boolean {
  return !!tasks?.some((t) => t.status === 'running' || t.status === 'pending');
}

/** SSE 实时进度：对每个 running/pending 任务开 EventSource，
 *  progress 帧 → 本地即时更新（不等 2s 轮询）；done 帧 → mutate 拉列表。
 *  连接失败自动降级（EventSource 内建重连；RQ worker 场景无事件，靠轮询兜底）。 */
type LiveFrame = { phase?: string | null; done?: number; rows?: number; failed?: number };

function useTaskEvents(tasks: DataTask[] | undefined, mutate: () => void) {
  const [live, setLive] = useState<Record<string, LiveFrame>>({});
  const activeIds = (tasks ?? [])
    .filter((t) => t.status === 'running' || t.status === 'pending')
    .map((t) => t.task_id)
    .sort()
    .join(',');

  useEffect(() => {
    if (!activeIds) return;
    // SSR / jsdom 无 EventSource：跳过 SSE，仅靠轮询兜底
    if (typeof EventSource === 'undefined') return;
    const sources = activeIds.split(',').map((id) => {
      const es = new EventSource(`/api/data/tasks/${id}/events`);
      const apply = (frame: LiveFrame) =>
        setLive((prev) => ({ ...prev, [id]: { ...prev[id], ...frame } }));
      es.addEventListener('snapshot', (e) =>
        apply(JSON.parse((e as MessageEvent).data as string)));
      es.addEventListener('progress', (e) =>
        apply(JSON.parse((e as MessageEvent).data as string)));
      es.addEventListener('done', (e) => {
        apply(JSON.parse((e as MessageEvent).data as string));
        es.close();
        void mutate();
      });
      es.onerror = () => {
        /* EventSource 自动重连；持续失败时 2s 轮询仍是兜底 */
      };
      return es;
    });
    return () => sources.forEach((s) => s.close());
  }, [activeIds, mutate]);

  return live;
}

export default function TasksPanel() {
  const { data: tasks, mutate } = useSWR<DataTask[]>('/data/tasks?limit=50', fetcher, {
    // SSE 主通道；轮询降为 5s 兜底（RQ worker 场景无 SSE 事件）
    refreshInterval: (latest?: DataTask[]) => (hasActive(latest) ? 5_000 : 60_000),
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [showBackfill, setShowBackfill] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const live = useTaskEvents(tasks, mutate);

  async function runDailyUpdate() {
    setBusy('daily');
    setMsg('');
    try {
      const r = await post<{ task_id: string }>('/data/tasks', {
        kind: 'daily_update',
        params: { days: 10 },
      });
      setMsg(`✓ 增量任务已创建（${r.task_id.slice(0, 8)}…），稍后看进度`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function retry(taskId: string) {
    setRetrying(taskId);
    setMsg('');
    try {
      const r = await post<{ task_id: string }>(`/data/tasks/${taskId}/retry`, {});
      setMsg(`✓ 补漏任务已创建（${r.task_id.slice(0, 8)}…），仅重跑失败标的`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setRetrying(null);
    }
  }

  async function cancel(taskId: string) {
    setBusy(taskId);
    setMsg('');
    try {
      await post<{ canceled: boolean }>(`/tasks/data/${taskId}/cancel`, {});
      setMsg(`✓ 已请求取消（${taskId.slice(0, 8)}…），任务将在下一个协作点退出`);
      void mutate();
    } catch (e) {
      // 409 = 任务已结束，属可忽略的竞态
      const m = e instanceof Error ? e.message : String(e);
      setMsg(m.includes('409') || m.includes('已结束') ? `任务已结束，无需取消（${taskId.slice(0, 8)}…）` : `✗ ${m}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <Panel
      title="数据任务"
      meta="全量回填 / 每日增量 · 单任务互斥"
      actions={
        <>
          <button className="btn btn-sm" onClick={() => setShowBackfill(true)} disabled={busy !== ''}>
            全量回填
          </button>
          <button className="btn btn-sm btn-primary" onClick={runDailyUpdate} disabled={busy !== ''}>
            {busy === 'daily' ? '创建中…' : '立即增量'}
          </button>
        </>
      }
    >
      <Msg text={msg} />
      {!!tasks?.length && <TasksSummaryChart tasks={tasks} />}
      {!tasks?.length ? (
        <Empty>暂无任务 —— 点右上角「全量回填」或「立即增量」创建</Empty>
      ) : (
        <div className="space-y-2">
          {tasks.map((t) => (
            <TaskRow
              key={t.task_id}
              task={t}
              live={live[t.task_id]}
              expanded={expanded === t.task_id}
              onToggle={() => setExpanded(expanded === t.task_id ? null : t.task_id)}
              onRetry={() => retry(t.task_id)}
              retrying={retrying === t.task_id}
              onCancel={() => cancel(t.task_id)}
              canceling={busy === t.task_id}
            />
          ))}
        </div>
      )}
      {showBackfill && (
        <BackfillModal
          onClose={() => setShowBackfill(false)}
          onCreated={(id) => {
            setShowBackfill(false);
            setMsg(`✓ 回填任务已创建（${id.slice(0, 8)}…），后台执行中`);
            void mutate();
          }}
        />
      )}
    </Panel>
  );
}

function TaskRow({ task, live, expanded, onToggle, onRetry, retrying, onCancel, canceling }: {
  task: DataTask;
  live?: LiveFrame;
  expanded: boolean;
  onToggle: () => void;
  onRetry: () => void;
  retrying: boolean;
  onCancel: () => void;
  canceling: boolean;
}) {
  const cancellable = task.status === 'running' || task.status === 'pending';
  const total = task.total_symbols ?? 0;
  const done = Math.max(task.done_symbols ?? 0, live?.done ?? 0);
  const rows = Math.max(task.rows_written ?? 0, live?.rows ?? 0);
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const canExpand =
    task.status === 'partial' || task.status === 'failed' || task.status === 'interrupted';

  return (
    <div className="border border-line bg-white">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
        {canExpand ? (
          <button
            onClick={onToggle}
            className="w-4 shrink-0 text-left text-ink-faint hover:text-ink"
            aria-label="展开失败明细"
          >
            {expanded ? '▾' : '▸'}
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <TaskStatusBadge status={task.status} />
        <span className="font-medium">{taskKindText(task.kind)}</span>
        <span className="font-mono text-xs text-ink-faint">{task.task_id.slice(0, 8)}</span>
        <span className="text-xs text-ink-faint">
          {task.params?.start ? `${String(task.params.start)} ~ ${String(task.params.end ?? '')}` : ''}
        </span>
        <div className="ml-auto flex items-center gap-3 text-xs tabular-nums text-ink-dim">
          <span>{rows.toLocaleString()} 行</span>
          <span>{taskElapsed(task.started_at, task.finished_at)}</span>
          {cancellable && (
            <button
              className="btn btn-sm"
              onClick={onCancel}
              disabled={canceling}
              aria-label={`取消任务 ${task.task_id.slice(0, 8)}`}
            >
              {canceling ? '取消中…' : '取消'}
            </button>
          )}
        </div>
      </div>
      {(total > 0 || task.status === 'running') && (
        <div className="px-3 pb-1.5">
          <div className="h-1.5 w-full bg-paper">
            <div className="h-1.5 bg-ink" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-faint">
            <span>{done}/{total} 标的</span>
            {task.phase && <span>· {task.phase}</span>}
            {task.failed_symbols.length > 0 && (
              <span className="text-up">失败 {task.failed_symbols.length}</span>
            )}
            {task.message && <span className="truncate">· {task.message}</span>}
          </div>
        </div>
      )}
      {expanded && canExpand && (
        <FailedDetailTable task={task} onRetry={onRetry} retrying={retrying} />
      )}
    </div>
  );
}

function FailedDetailTable({ task, onRetry, retrying }: {
  task: DataTask;
  onRetry: () => void;
  retrying: boolean;
}) {
  return (
    <div className="border-t border-line bg-paper px-3 py-2">
      {!task.failed_detail.length ? (
        <div className="py-2 text-xs text-ink-faint">无失败明细</div>
      ) : (
        <>
          <table className="table-dense">
            <thead>
              <tr>
                <th className="w-24 text-left">标的</th>
                <th className="text-left">原因</th>
              </tr>
            </thead>
            <tbody>
              {task.failed_detail.map((f) => (
                <tr key={f.symbol}>
                  <td className="font-mono text-xs">{f.symbol}</td>
                  <td className="text-xs text-ink-dim">{f.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* retry 是任务级（重跑全部失败标的），不做逐行操作 */}
          <div className="mt-2">
            <button className="btn btn-sm" onClick={onRetry} disabled={retrying}>
              {retrying ? '补漏中…' : `重试全部失败标的（${task.failed_detail.length}）`}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
