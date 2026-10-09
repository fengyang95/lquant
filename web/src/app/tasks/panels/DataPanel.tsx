'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';
import { taskKindText } from '@/app/data/TaskBadge';
import type { DataTask } from '@/app/data/types';
import BackfillModal from '@/app/data/BackfillModal';
import { paramsBrief } from '../types';

/** 进行中（running/pending）→ 2s 轮询，否则 60s 慢刷 */
function hasActive(tasks: DataTask[] | undefined): boolean {
  return !!tasks?.some((t) => t.status === 'running' || t.status === 'pending');
}

/** 数据任务面板：列表（SWR 轮询）+ 全量回填 / 立即增量 + 失败明细展开 + 可编辑参数重试 */
export default function DataPanel() {
  const { data: tasks, mutate } = useSWR<DataTask[]>('/data/tasks?limit=50', fetcher, {
    refreshInterval: (latest?: DataTask[]) => (hasActive(latest) ? 2_000 : 60_000),
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [showBackfill, setShowBackfill] = useState(false);
  // 重试弹窗：任务 id + JSON 文本（默认回填原 params）
  const [retry, setRetry] = useState<{ id: string; text: string } | null>(null);

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

  /** 协作式取消运行中的任务（走 /tasks/data/{id}/cancel，与其余四类任务面板一致）。
   *  409 = 任务已结束，属可忽略的竞态，提示而非报错。 */
  async function cancelTask(id: string) {
    setBusy(id);
    setMsg('');
    try {
      await post<{ canceled: boolean }>(`/tasks/data/${id}/cancel`, {});
      setMsg(`✓ 已请求取消（${id.slice(0, 8)}…），任务将在下一个协作点退出`);
      void mutate();
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setMsg(m.includes('409') || m.includes('已结束')
        ? `任务已结束，无需取消（${id.slice(0, 8)}…）`
        : `✗ ${m}`);
    } finally {
      setBusy('');
    }
  }

  async function submitRetry() {
    if (!retry) return;
    let params: Record<string, unknown> = {};
    try {
      params = retry.text.trim() ? JSON.parse(retry.text) as Record<string, unknown> : {};
    } catch {
      setMsg('✗ 参数不是合法 JSON');
      return;
    }
    setBusy(retry.id);
    setMsg('');
    try {
      const r = await post<{ task_id: string }>(`/tasks/data/${retry.id}/retry`, { params });
      setMsg(`✓ 重试任务已创建（${r.task_id.slice(0, 8)}…）`);
      setRetry(null);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
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
      {!tasks?.length ? (
        <div className="border border-dashed border-line-strong bg-panel px-6 py-12 text-center text-sm text-ink-faint">
          暂无任务 —— 点右上角「全量回填」或「立即增量」创建
        </div>
      ) : (
        <div className="space-y-2">
          {tasks.map((t) => {
            const total = t.total_symbols ?? 0;
            const done = t.done_symbols ?? 0;
            const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
            const canExpand =
              t.status === 'partial' || t.status === 'failed' || t.status === 'interrupted';
            return (
              <div key={t.task_id} className="border border-line bg-white">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                  {canExpand ? (
                    <button
                      onClick={() => setExpanded(expanded === t.task_id ? null : t.task_id)}
                      className="w-4 shrink-0 text-left text-ink-faint hover:text-ink"
                      aria-label="展开失败明细"
                    >
                      {expanded === t.task_id ? '▾' : '▸'}
                    </button>
                  ) : (
                    <span className="w-4 shrink-0" />
                  )}
                  <span className="font-medium">{taskKindText(t.kind)}</span>
                  <span className="font-mono text-xs text-ink-faint">{t.task_id.slice(0, 8)}</span>
                  <span className="max-w-64 truncate font-mono text-xs text-ink-dim">
                    {paramsBrief(t.params as Record<string, unknown>) || '—'}
                  </span>
                  <div className="ml-auto flex items-center gap-3 text-xs tabular-nums text-ink-dim">
                    <span>
                      {done}/{total} 标的
                    </span>
                    {t.message && <span className="max-w-52 truncate" title={t.message}>{t.message}</span>}
                    {(t.status === 'running' || t.status === 'pending') && (
                      <button
                        className="btn btn-sm border-up/40 text-up hover:bg-paper"
                        aria-label={`取消任务 ${t.task_id.slice(0, 8)}`}
                        onClick={() => cancelTask(t.task_id)}
                        disabled={busy !== ''}
                      >
                        {busy === t.task_id ? '取消中…' : '取消'}
                      </button>
                    )}
                    {(t.status === 'failed' || t.status === 'partial' || t.status === 'interrupted') && (
                      <button
                        className="btn btn-sm"
                        onClick={() =>
                          setRetry({
                            id: t.task_id,
                            text: JSON.stringify((t.params ?? {}) as Record<string, unknown>, null, 2),
                          })
                        }
                      >
                        重试
                      </button>
                    )}
                  </div>
                </div>
                {(total > 0 || t.status === 'running') && (
                  <div className="px-3 pb-1.5">
                    <div className="h-1.5 w-full bg-paper">
                      <div className="h-1.5 bg-ink" style={{ width: `${pct}%` }} />
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-faint">
                      <span>{t.phase && <span>· {t.phase}</span>}</span>
                    </div>
                  </div>
                )}
                {expanded === t.task_id && canExpand && t.failed_detail.length > 0 && (
                  <div className="border-t border-line bg-paper px-3 py-2">
                    <table className="table-dense">
                      <thead>
                        <tr>
                          <th className="w-24 text-left">标的</th>
                          <th className="text-left">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {t.failed_detail.map((f) => (
                          <tr key={f.symbol}>
                            <td className="font-mono text-xs">{f.symbol}</td>
                            <td className="text-xs text-ink-dim">{f.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
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
      {retry && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setRetry(null)}>
          <div className="w-full max-w-md border border-line bg-paper p-5 shadow-lg" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-1 font-song text-lg font-semibold">重试任务</h3>
            <p className="mb-3 text-xs text-ink-faint">可编辑参数后重跑（仅重跑失败标的）。</p>
            <textarea
              value={retry.text}
              onChange={(e) => setRetry({ ...retry, text: e.target.value })}
              rows={8}
              className="input input-mono w-full"
            />
            <div className="mt-3 flex justify-end gap-2">
              <button className="btn" onClick={() => setRetry(null)} disabled={busy !== ''}>
                取消
              </button>
              <button className="btn btn-primary" onClick={submitRetry} disabled={busy !== ''}>
                {busy !== '' ? '提交中…' : '创建重试任务'}
              </button>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
