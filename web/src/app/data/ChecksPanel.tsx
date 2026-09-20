'use client';

import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote } from '@/components/States';
import { fetcher } from '@/lib/api';
import { latestChecksByKind, type CheckRun, type CheckSummary } from './checks';

const TONE_TEXT: Record<CheckSummary['tone'], string> = {
  ok: 'text-down',
  warn: 'text-gold',
  bad: 'text-up',
  unknown: 'text-ink-faint',
};

const TONE_DOT: Record<CheckSummary['tone'], string> = {
  ok: 'bg-down',
  warn: 'bg-gold',
  bad: 'bg-up',
  unknown: 'bg-ink-faint',
};

/** 完备性检查结果区：从 /sync/history 的 detail.checks 汇总各 kind 最近一次检查。
 *  只读 —— 修复动作（补齐/重跑）在 /sync。 */
export default function ChecksPanel() {
  const { data, error, isLoading } = useSWR<CheckRun[]>('/sync/history?limit=50', fetcher, {
    refreshInterval: 30_000,
    shouldRetryOnError: false,
  });

  const summaries = latestChecksByKind(data ?? []);

  return (
    <Panel title="完备性检查" meta="各数据任务最近一次同步后检查（覆盖度 / 断点对账）">
      {error ? (
        <ErrorNote>检查结果加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-4 text-sm text-ink-faint">加载中…</p>
      ) : !summaries.length ? (
        <Empty>暂无检查记录 —— 作业在 /sync 跑过一轮后这里会显示检查结论</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {summaries.map((s) => (
            <li
              key={s.kind}
              className={`flex items-center justify-between gap-3 border-l-2 px-2 py-2 text-sm ${
                s.tone === 'bad' ? 'border-up bg-up/5' : 'border-transparent'
              }`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${TONE_DOT[s.tone]}`} />
                <span className="font-medium">{s.label}</span>
                <span className="text-xs text-ink-faint">
                  {s.started_at ? s.started_at.slice(5, 16) : ''}
                </span>
              </div>
              <span className={`shrink-0 text-xs ${TONE_TEXT[s.tone]}`}>{s.text}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
