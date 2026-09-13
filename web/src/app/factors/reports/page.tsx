'use client';

/**
 * 因子报告中心 —— 全部评价/合成/挖掘报告列表，点击新窗口打开 HTML 报告。
 * 客户端按因子名前缀过滤。
 */
import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, Loading } from '@/components/States';
import { get } from '@/lib/api';

type ReportRow = { name: string; url: string; size_kb: number };

export default function FactorReportsPage() {
  const { data, isLoading } = useSWR<ReportRow[]>('/factors/reports', get);
  const [q, setQ] = useState('');

  const shown = (data ?? []).filter((r) => r.name.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div className="space-y-5">
      <PageHeader title="因子报告" sub="评价 · 合成 · 全部 HTML 报告汇总 · 点击新窗口打开" />
      <Panel title="报告列表" meta={`共 ${shown.length} 份`}>
        <div className="mb-3">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按报告名过滤，如 pct_change_20 或 syn_"
            className="input w-72 py-1 text-xs"
          />
        </div>
        {isLoading ? (
          <Loading />
        ) : shown.length === 0 ? (
          <Empty>暂无报告 —— 到因子页跑一次评价或合成即生成</Empty>
        ) : (
          <ul className="divide-y divide-line text-sm">
            {shown.map((r) => (
              <li key={r.name} className="flex items-center justify-between py-2">
                <a href={r.url} target="_blank" className="font-mono text-indigo hover:underline" rel="noreferrer">
                  {r.name} ↗
                </a>
                <span className="text-xs text-ink-faint">{r.size_kb} KB</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
