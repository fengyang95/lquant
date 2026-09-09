'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';
import { taskElapsed, TaskStatusBadge, taskKindText } from './TaskBadge';
import BackfillModal from './BackfillModal';
import type { DataTask } from './types';

/** 进行中（running/pending）任务存在 → 列表 2s 轮询（spec 允许的轮询路径） */
function hasActive(tasks: DataTask[] | undefined): boolean {
  return !!tasks?.some((t) => t.status === 'running' || t.status === 'pending');
}

export default function TasksPanel() {
  const { data: tasks, mutate } = useSWR<DataTask[]>('/data/tasks?limit=50', fetcher, {
    // 有 running/pending → 2s 轮询；否则 60s 慢刷（SWR 2.x 支持按最新数据取间隔）
    refreshInterval: (latest?: DataTask[]) => (hasActive(latest) ? 2_000 : 60_000),
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [showBackfill, setShowBackfill] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);

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
      {!tasks?.length ? (
        <Empty>暂无任务 —— 点右上角「全量回填」或「立即增量」创建</Empty>
      ) : (
        <div className="space-y-2">
          {tasks.map((t) => (
            <TaskRow
              key={t.task_id}
              task={t}
              expanded={expanded === t.task_id}
              onToggle={() => setExpanded(expanded === t.task_id ? null : t.task_id)}
              onRetry={() => retry(t.task_id)}
              retrying={retrying === t.task_id}
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

function TaskRow({ task, expanded, onToggle, onRetry, retrying }: {
  task: DataTask;
  expanded: boolean;
  onToggle: () => void;
  onRetry: () => void;
  retrying: boolean;
}) {
  const total = task.total_symbols ?? 0;
  const done = task.done_symbols ?? 0;
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
          <span>{(task.rows_written ?? 0).toLocaleString()} 行</span>
          <span>{taskElapsed(task.started_at, task.finished_at)}</span>
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
        <table className="table-dense">
          <thead>
            <tr>
              <th className="w-24 text-left">标的</th>
              <th className="text-left">原因</th>
              <th className="w-16 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {task.failed_detail.map((f) => (
              <tr key={f.symbol}>
                <td className="font-mono text-xs">{f.symbol}</td>
                <td className="text-xs text-ink-dim">{f.reason}</td>
                <td className="text-right">
                  <button className="btn btn-sm" onClick={onRetry} disabled={retrying}>
                    {retrying ? '…' : '重试'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
