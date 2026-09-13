'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import { del, fetcher } from '@/lib/api';

type CheckpointInfo = {
  name: string;
  done: number;
  updated_at: string | null;
  meta: Record<string, unknown>;
};

function metaText(meta: Record<string, unknown>): string {
  const parts = Object.entries(meta)
    .filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => `${k}=${String(v)}`);
  return parts.join(' · ');
}

/** 断点续传管理卡：查看 data/cache/checkpoints 下的断点（CLI 与前端任务共用），
 *  支持归档（重命名保留，不参与续传）—— 换日期区间回填前必须先清/归档同名断点。 */
export default function CheckpointPanel() {
  const { data: cps, error, mutate } = useSWR<CheckpointInfo[]>('/data/checkpoints', fetcher, {
    refreshInterval: 30_000,
  });
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');

  async function archive(name: string) {
    // 归档影响断点续传：CLI 正在跑的回填会失去进度记账，须用户确认
    if (!window.confirm(`归档断点「${name}」？归档后不再参与续传（文件重命名保留，可追溯）。若 CLI 回填正在进行，请勿归档。`)) {
      return;
    }
    setBusy(name);
    setMsg('');
    try {
      const r = await del<{ name: string; archived_to: string }>(`/data/checkpoints/${encodeURIComponent(name)}`);
      setMsg(`✓ 已归档 ${r.name} → ${r.archived_to}`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <Panel title="断点续传" meta="CLI 与前端任务共用 · 归档 = 重命名保留，不再续传">
      <Msg text={msg} />
      {error ? (
        <Empty>加载断点列表失败：{error instanceof Error ? error.message : String(error)}</Empty>
      ) : !cps?.length ? (
        <Empty>暂无断点文件 —— 回填/批量补数运行后这里出现各任务的断点</Empty>
      ) : (
        <table className="table-dense">
          <thead>
            <tr>
              <th className="text-left">断点</th>
              <th className="w-24 text-right">已完成</th>
              <th className="w-44 text-left">更新时间</th>
              <th className="text-left">元信息</th>
              <th className="w-16 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {cps.map((c) => (
              <tr key={c.name}>
                <td className="font-mono text-xs">{c.name}</td>
                <td className="text-right font-mono text-xs tabular-nums">{c.done.toLocaleString()}</td>
                <td className="text-xs text-ink-faint">{c.updated_at ?? '—'}</td>
                <td className="max-w-60 truncate text-xs text-ink-dim" title={metaText(c.meta)}>
                  {metaText(c.meta) || '—'}
                </td>
                <td className="text-right">
                  <button
                    className="btn btn-sm"
                    onClick={() => archive(c.name)}
                    disabled={busy === c.name}
                    title="归档后不再参与断点续传"
                  >
                    {busy === c.name ? '归档中…' : '归档'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
