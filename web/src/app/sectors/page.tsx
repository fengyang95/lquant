'use client';

import useSWR from 'swr';
import { get } from '@/lib/api';

type SectorRow = {
  trade_date: string;
  sector_name: string;
  change_pct: number;
  main_net_inflow: number;
  up_count: number | null;
  down_count: number | null;
};

export default function SectorsPage() {
  const { data, error, isLoading } = useSWR<SectorRow[]>('/market/sectors', get, {
    refreshInterval: 60_000,
  });

  if (isLoading) return <div className="py-20 text-center text-neutral-400">加载中…</div>;
  if (error) return <div className="py-20 text-center text-red-500">加载失败</div>;
  if (!data?.length)
    return (
      <div className="rounded-xl border border-dashed bg-white py-20 text-center text-neutral-400">
        暂无板块数据 —— 先 POST /api/market/collect 触发采集
      </div>
    );

  const maxAbs = Math.max(...data.map((s) => Math.abs(s.change_pct)), 1);

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">板块行情</h1>
        <span className="text-sm text-neutral-400">{data[0]?.trade_date}</span>
      </div>
      <div className="rounded-xl border bg-white p-4">
        <table className="w-full text-sm">
          <thead className="text-xs text-neutral-400">
            <tr className="border-b">
              <th className="py-1.5 text-left font-normal">板块</th>
              <th className="text-left font-normal">涨跌幅</th>
              <th className="text-right font-normal">主力净流入（亿）</th>
              <th className="text-right font-normal">涨/跌家数</th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => {
              const w = (Math.abs(s.change_pct) / maxAbs) * 100;
              const bg = s.change_pct >= 0 ? 'bg-red-500' : 'bg-green-500';
              return (
                <tr key={s.sector_name} className="border-b border-neutral-50">
                  <td className="py-1.5 font-medium">{s.sector_name}</td>
                  <td>
                    <div className="relative h-5 w-40">
                      <div
                        className={`absolute top-0.5 h-4 ${bg} opacity-15`}
                        style={{ width: `${w}%` }}
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
                  <td className="text-right text-xs tabular-nums text-neutral-500">
                    <span className="text-up">{s.up_count ?? '—'}</span> / <span className="text-down">{s.down_count ?? '—'}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
