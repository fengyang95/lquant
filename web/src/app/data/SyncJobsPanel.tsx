'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';
import type { SyncJob, SyncRunRecord } from './types';

/** 运行状态徽章：ok 绿（红涨绿跌）/ partial 金 / failed 红 */
function runStatusClass(s: string): string {
  if (s === 'ok') return 'text-down';
  if (s === 'partial') return 'text-gold';
  if (s === 'failed') return 'text-up';
  return 'text-ink-faint';
}

/** 同步作业管理：GET /sync/jobs + /sync/history，启停 toggle、立即执行 POST /sync/run。 */
export default function SyncJobsPanel() {
  const { data: jobs, error, mutate } = useSWR<SyncJob[]>('/sync/jobs', fetcher, {
    refreshInterval: 60_000,
  });
  const { data: history, mutate: mutateHistory } = useSWR<SyncRunRecord[]>(
    '/sync/history?limit=20',
    fetcher,
    { refreshInterval: 30_000 },
  );
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');

  async function toggle(job: SyncJob) {
    setBusy(`toggle-${job.sync_id}`);
    setMsg('');
    try {
      await post(`/sync/jobs/${job.sync_id}/toggle`, { enabled: !job.enabled });
      setMsg(`✓ ${job.name} 已${job.enabled ? '停用' : '启用'}`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function runNow(job: SyncJob) {
    setBusy(`run-${job.sync_id}`);
    setMsg('');
    try {
      await post('/sync/run', { sync_id: job.sync_id });
      setMsg(`✓ ${job.name} 已触发，执行中 —— 运行历史稍后刷新可见`);
      void mutateHistory();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <Panel title="同步作业" meta="调度器作业启停 / 手动触发">
      {error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : !jobs?.length ? (
        <Empty>暂无同步作业</Empty>
      ) : (
        <table className="table-dense">
          <thead>
            <tr>
              <th className="text-left">作业</th>
              <th className="w-24 text-left">kind</th>
              <th className="w-28 text-left">调度</th>
              <th className="w-16 text-left">状态</th>
              <th className="w-44 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.sync_id}>
                <td>
                  <span className="font-medium">{j.name}</span>
                  <span className="ml-2 font-mono text-xs text-ink-faint">{j.sync_id}</span>
                </td>
                <td className="font-mono text-xs">{j.kind}</td>
                <td className="font-mono text-xs">
                  {j.schedule_time}（周{j.weekdays}）
                </td>
                <td className={`text-xs ${j.enabled ? 'text-down' : 'text-ink-faint'}`}>
                  {j.enabled ? '启用' : '停用'}
                </td>
                <td>
                  <div className="flex justify-end gap-2">
                    <button
                      className="btn btn-sm"
                      onClick={() => toggle(j)}
                      disabled={busy !== ''}
                    >
                      {busy === `toggle-${j.sync_id}` ? '…' : j.enabled ? '停用' : '启用'}
                    </button>
                    <button
                      className="btn btn-sm btn-primary"
                      onClick={() => runNow(j)}
                      disabled={busy !== ''}
                    >
                      {busy === `run-${j.sync_id}` ? '执行中…' : '立即执行'}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {history && history.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-xs text-ink-faint">最近运行</h3>
          <div className="max-h-64 overflow-auto">
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="w-16 text-left">状态</th>
                  <th className="text-left">作业</th>
                  <th className="w-36 text-left">开始</th>
                  <th className="w-24 text-right">行数</th>
                  <th className="text-left">备注</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h, idx) => (
                  <tr key={`${h.sync_id}-${h.started_at ?? ''}-${idx}`}>
                    <td className={`text-xs ${runStatusClass(h.status)}`}>{h.status}</td>
                    <td className="text-xs">{h.name || h.sync_id}</td>
                    <td className="text-xs text-ink-faint">{h.started_at?.slice(0, 19) ?? '—'}</td>
                    <td className="text-right font-mono text-xs">
                      {h.rows == null ? '—' : h.rows.toLocaleString()}
                    </td>
                    <td className="max-w-60 truncate text-xs text-ink-dim" title={h.error ?? ''}>
                      {h.error ?? ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <Msg text={msg} />
    </Panel>
  );
}
