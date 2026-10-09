'use client';

import { useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { get } from '@/lib/api';

type SectorRow = {
  trade_date: string;
  sector_name: string;
  change_pct: number;
  main_net_inflow: number;
  up_count: number | null;
  down_count: number | null;
};

const KINDS = [
  { key: 'industry', label: '行业' },
  { key: 'concept', label: '概念' },
  { key: 'area', label: '地域' },
] as const;

type Kind = (typeof KINDS)[number]['key'];

export default function SectorsPage() {
  const [kind, setKind] = useState<Kind>('industry');
  const { data, error, isLoading } = useSWR<SectorRow[]>(
    `/market/sectors?kind=${kind}`,
    get,
    { refreshInterval: 60_000 },
  );

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

  const rows = data ?? [];
  const latestDate = rows[0]?.trade_date;

  return (
    <div className="space-y-5">
      <PageHeader
        title="板块"
        sub={
          <>
            当日快照：板块涨跌与主力净流入 · {latestDate ?? '暂无数据'}
            {' '}· 需要历史比较与景气/估值分析请用
            <Link href="/industry" className="ml-1 underline hover:text-ink">行业分析</Link>
          </>
        }
      />

      <div className="flex gap-2">
        {KINDS.map((k) => (
          <button
            key={k.key}
            type="button"
            onClick={() => setKind(k.key)}
            className={`rounded px-3 py-1 text-sm transition-colors ${
              kind === k.key
                ? 'bg-ink text-paper'
                : 'bg-paper text-ink-dim hover:text-ink'
            }`}
          >
            {k.label}
            {k.key === 'concept' && <span className="ml-1 text-xs opacity-60">(多)</span>}
          </button>
        ))}
      </div>

      {!rows.length ? (
        <Empty>
          暂无板块数据 —— 先 <code className="bg-paper px-1">POST /api/market/collect</code> 触发采集
        </Empty>
      ) : (
        <Panel bodyClass="">
          <table className="table-dense">
            <thead>
              <tr>
                <th className="pl-4 text-left">板块</th>
                <th className="text-left">涨跌幅</th>
                <th className="text-right">主力净流入（亿）</th>
                <th className="pr-4 text-right">涨/跌家数</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const w = (Math.abs(s.change_pct) / maxAbs(rows)) * 100;
                const bg = s.change_pct >= 0 ? 'rgba(195,53,43,.12)' : 'rgba(30,124,85,.12)';
                return (
                  <tr key={s.sector_name} className="hover:bg-white">
                    <td className="pl-4 font-medium">{s.sector_name}</td>
                    <td>
                      <div className="relative h-5 w-40">
                        <div
                          className="absolute top-0.5 h-4"
                          style={{ width: `${w}%`, background: bg }}
                        />
                        <span className={`relative text-xs font-medium ${s.change_pct >= 0 ? 'text-up' : 'text-down'}`}>
                          {s.change_pct >= 0 ? '+' : ''}
                          {s.change_pct.toFixed(2)}%
                        </span>
                      </div>
                    </td>
                    <td className={`text-right tabular-nums ${s.main_net_inflow >= 0 ? 'text-up' : 'text-down'}`}>
                      {(s.main_net_inflow / 1e8).toFixed(2)}
                    </td>
                    <td className="pr-4 text-right text-xs tabular-nums text-ink-dim">
                      <span className="text-up">{s.up_count ?? '—'}</span> / <span className="text-down">{s.down_count ?? '—'}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

function maxAbs(rows: SectorRow[]): number {
  return Math.max(...rows.map((s) => Math.abs(s.change_pct)), 1);
}
