'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote } from '@/components/States';
import { fetcher } from '@/lib/api';
import type { DataIssue } from './types';

const SEV_CLASS: Record<string, string> = {
  fatal: 'bg-up/10 text-up font-semibold',
  error: 'bg-up/10 text-up',
  warn: 'bg-gold/10 text-gold',
  info: 'bg-ink-faint/10 text-ink-faint',
};

/** 最近质量问题（概览版 IssuesPanel）：fatal/error 级高亮，点击行内展开全文。
 *  只读；批量处理等操作在完整版（历史模式）里做。 */
export default function RecentIssuesPanel() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, error, isLoading } = useSWR<DataIssue[]>(
    '/data/issues?resolved=false&limit=100',
    fetcher,
    { refreshInterval: 60_000, shouldRetryOnError: false },
  );

  const recent = (data ?? [])
    .filter((i) => i.severity === 'fatal' || i.severity === 'error')
    .slice(0, 8);

  function toggle(id: string) {
    setExpanded((prev) => (prev === id ? null : id));
  }

  return (
    <Panel title="最近质量问题" meta="fatal / error 级 · 未处理 · 点击展开详情">
      {error ? (
        <ErrorNote>质量问题加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-4 text-sm text-ink-faint">加载中…</p>
      ) : !recent.length ? (
        <Empty>暂无未处理的 fatal/error 质量问题</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {recent.map((it) => {
            const id = String(it.id);
            const isOpen = expanded === id;
            return (
              <li key={id}>
                <button
                  type="button"
                  onClick={() => toggle(id)}
                  className="flex w-full items-center justify-between gap-3 px-2 py-2 text-left text-sm hover:bg-white"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className={`shrink-0 px-1.5 py-0.5 text-xs rounded-[2px] ${SEV_CLASS[it.severity] ?? SEV_CLASS.info}`}>
                      {it.severity}
                    </span>
                    <span className="font-mono text-xs">{it.rule_code}</span>
                    <span className="truncate text-xs text-ink-dim">{it.message}</span>
                  </div>
                  <span className="shrink-0 text-xs text-ink-faint">{it.created_at.slice(0, 16)}</span>
                </button>
                {isOpen && (
                  <div className="border-t border-line bg-paper px-3 py-2 text-xs text-ink-dim">
                    <div className="font-mono">#{id} · {it.dataset}</div>
                    <div className="mt-1 whitespace-pre-wrap">{it.message}</div>
                    {it.detail != null && (
                      <pre className="mt-1 overflow-auto font-mono text-[11px] text-ink-faint">
                        {JSON.stringify(it.detail, null, 2)}
                      </pre>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
