'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';
import type { DataIssue } from './types';

const SEV_CLASS: Record<string, string> = {
  fatal: 'bg-up/10 text-up font-semibold',
  error: 'bg-up/10 text-up',
  warn: 'bg-gold/10 text-gold',
  info: 'bg-ink-faint/10 text-ink-faint',
};

function buildQuery(f: { severity: string; dataset: string; resolved: string }): string {
  const q = new URLSearchParams();
  if (f.severity) q.set('severity', f.severity);
  if (f.dataset) q.set('dataset', f.dataset);
  if (f.resolved !== '') q.set('resolved', f.resolved);
  q.set('limit', '200');
  return q.toString();
}

/** 质量问题历史：GET /data/issues（severity/dataset/resolved 过滤）+ 批量 resolve。
 *  与 QualityPanel 的「即时检查」互补 —— 这里看的是落库历史。 */
export default function IssuesPanel() {
  const [filters, setFilters] = useState({ severity: '', dataset: '', resolved: 'false' });
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const query = useMemo(() => buildQuery(filters), [filters]);
  const { data, error, isLoading, mutate } = useSWR<DataIssue[]>(`/data/issues?${query}`, fetcher);

  const issues = data ?? [];
  const allChecked = issues.length > 0 && issues.every((i) => checked.has(String(i.id)));

  function setFilter(patch: Partial<typeof filters>) {
    setFilters({ ...filters, ...patch });
    setChecked(new Set());
  }

  function toggle(id: string) {
    const next = new Set(checked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setChecked(next);
  }

  async function resolveChecked() {
    if (!checked.size) return;
    setBusy(true);
    setMsg('');
    try {
      const r = await post<{ resolved: number }>('/data/issues/resolve', {
        ids: [...checked],
      });
      setMsg(`✓ 已标记 ${r.resolved} 条为已处理`);
      setChecked(new Set());
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="质量问题历史"
      meta="data_quality_issue 落库历史 · 勾选后可批量标记已处理"
      actions={
        <button className="btn btn-sm" onClick={resolveChecked} disabled={busy || !checked.size}>
          {busy ? '提交中…' : `标记已处理（${checked.size}）`}
          <span aria-hidden className="hidden" />
        </button>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select
          aria-label="severity 过滤"
          className="input w-auto text-sm"
          value={filters.severity}
          onChange={(e) => setFilter({ severity: e.target.value })}
        >
          <option value="">全部级别</option>
          <option value="fatal">fatal</option>
          <option value="error">error</option>
          <option value="warn">warn</option>
          <option value="info">info</option>
        </select>
        <select
          aria-label="dataset 过滤"
          className="input w-auto text-sm"
          value={filters.dataset}
          onChange={(e) => setFilter({ dataset: e.target.value })}
        >
          <option value="">全部数据集</option>
          <option value="daily">daily</option>
        </select>
        <select
          aria-label="resolved 过滤"
          className="input w-auto text-sm"
          value={filters.resolved}
          onChange={(e) => setFilter({ resolved: e.target.value })}
        >
          <option value="false">未处理</option>
          <option value="true">已处理</option>
          <option value="">全部</option>
        </select>
      </div>

      {error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-6 text-sm text-ink-faint">加载中…</p>
      ) : !issues.length ? (
        <Empty>没有符合条件的 issue —— 跑一次「质量检查」后这里会有落库记录</Empty>
      ) : (
        <div className="max-h-96 overflow-auto">
          <table className="table-dense">
            <thead>
              <tr>
                <th className="w-8 text-left">
                  <input
                    aria-label="全选"
                    type="checkbox"
                    checked={allChecked}
                    onChange={() =>
                      setChecked(allChecked ? new Set() : new Set(issues.map((i) => String(i.id))))
                    }
                  />
                </th>
                <th className="w-16 text-left">级别</th>
                <th className="text-left">规则</th>
                <th className="w-20 text-left">数据集</th>
                <th className="text-left">说明</th>
                <th className="w-36 text-left">时间</th>
              </tr>
            </thead>
            <tbody>
              {issues.map((it) => {
                const id = String(it.id);
                return (
                  <tr key={id} className={it.resolved ? 'opacity-50' : undefined}>
                    <td>
                      <input
                        aria-label={`勾选 ${id}`}
                        type="checkbox"
                        checked={checked.has(id)}
                        onChange={() => toggle(id)}
                      />
                    </td>
                    <td>
                      <span className={`px-1.5 py-0.5 text-xs rounded-[2px] ${SEV_CLASS[it.severity] ?? SEV_CLASS.info}`}>
                        {it.severity}
                      </span>
                    </td>
                    <td className="font-mono text-xs">{it.rule_code}</td>
                    <td className="text-xs">{it.dataset}</td>
                    <td className="max-w-80 truncate text-xs text-ink-dim" title={it.message}>{it.message}</td>
                    <td className="text-xs text-ink-faint">{it.created_at.slice(0, 19)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <Msg text={msg} />
    </Panel>
  );
}
