// 行业分位表 —— 数据源 GET /fundamental/percentiles。
//
// 这个端点此前在后端存在但前端零引用：分数页面只给出「你得了几分」，
// 却不给「这个档位是按什么分布划的」。行业分位是评分体系的**核心口径**，
// 看不到分布就无法判断一个 80 分到底意味着什么。
'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { get } from '@/lib/api';

export type PercentileRow = {
  industry: string;
  item: string;
  label?: string;
  module?: string;
  p25: number;
  p50: number;
  p75: number;
  n: number;
};

type Resp = {
  asof: string;
  available: boolean;
  hint?: string;
  rows: PercentileRow[];
};

export default function PercentileMatrix({
  asof,
  minSamples = 5,
}: {
  asof?: string;
  minSamples?: number;
}) {
  const query = `/fundamental/percentiles?min_samples=${minSamples}`
    + (asof ? `&asof=${asof}` : '');
  const { data, isLoading, error } = useSWR<Resp>(query, get);
  const [item, setItem] = useState('');

  const rows = useMemo(() => data?.rows ?? [], [data]);
  const items = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of rows) if (!seen.has(r.item)) seen.set(r.item, r.label ?? r.item);
    return [...seen.entries()].map(([key, label]) => ({ key, label }));
  }, [rows]);

  const current = item || items[0]?.key || '';
  const detail = useMemo(
    () => rows.filter((r) => r.item === current).sort((a, b) => b.n - a.n),
    [rows, current],
  );

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>分位加载失败：{String(error)}</ErrorNote>;
  if (!data?.available || rows.length === 0) {
    return <Empty>{data?.hint ?? '该观察日没有可用分位（行业样本不足）'}</Empty>;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm">
          <span className="mr-2 text-xs text-ink-faint">指标</span>
          <select value={current} onChange={(e) => setItem(e.target.value)}
                  className="input" aria-label="分位指标">
            {items.map((o) => (
              <option key={o.key} value={o.key}>{o.label}</option>
            ))}
          </select>
        </label>
        <span className="text-xs text-ink-faint">
          观察日 {data.asof} · 样本不足 {minSamples} 的行业不出分位
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="table-dense">
          <thead>
            <tr>
              <th className="text-left">行业</th>
              <th className="text-right">P25</th>
              <th className="text-right">P50</th>
              <th className="text-right">P75</th>
              <th className="text-right">样本数</th>
            </tr>
          </thead>
          <tbody>
            {detail.map((r) => (
              <tr key={`${r.industry}-${r.item}`} className="hover:bg-white">
                <td>{r.industry}</td>
                <td className="text-right tabular-nums text-ink-dim">{r.p25.toFixed(2)}</td>
                <td className="text-right tabular-nums font-medium">{r.p50.toFixed(2)}</td>
                <td className="text-right tabular-nums text-ink-dim">{r.p75.toFixed(2)}</td>
                <td className="text-right tabular-nums text-ink-faint">{r.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-ink-faint">
        档位口径：正向指标 ≥P75 得满分，≥P50 得 80%，≥P25 得 50%，其余 20%；
        反向指标（周转天数、PE、PB、资产负债率）方向对称。
      </p>
    </div>
  );
}
