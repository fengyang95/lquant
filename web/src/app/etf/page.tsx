'use client';

import useSWR from 'swr';
import { getData } from '@/lib/api';

type EtfMeta = {
  symbol: string;
  name: string | null;
  track_index: string | null;
  fund_type: string | null;
  is_cross_border: boolean;
  sellable_after_days: number | null;
  management_fee: number | null;
  custody_fee: number | null;
  fund_size: number | null;
  as_of: string | null;
};

type CorrPair = { a: string; b: string; corr: number };
type Corr = { symbols: string[]; pairs: CorrPair[]; note: string | null };

export default function EtfPage() {
  const { data: meta } = useSWR<EtfMeta[]>('/etf/meta', getData, { refreshInterval: 60_000 });
  const { data: corr } = useSWR<Corr>('/etf/correlation', getData, { refreshInterval: 300_000 });

  const cross = meta?.filter((m) => m.is_cross_border) ?? [];
  const local = meta?.filter((m) => !m.is_cross_border) ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">ETF 专区</h1>
        <span className="text-xs text-neutral-400">元数据 · 相关性 · 跨境/境内</span>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border bg-white p-4 lg:col-span-2">
          <div className="mb-2 text-sm font-medium">ETF 元数据（{meta?.length ?? 0}）</div>
          {!meta?.length ? (
            <div className="py-10 text-center text-sm text-neutral-400">暂无 ETF 元数据 —— 先同步 ETF 元数据表</div>
          ) : (
            <div className="max-h-96 overflow-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-white text-xs text-neutral-400">
                  <tr className="border-b">
                    <th className="py-1.5 text-left font-normal">代码</th>
                    <th className="text-left font-normal">名称</th>
                    <th className="text-left font-normal">跟踪指数</th>
                    <th className="text-left font-normal">类型</th>
                    <th className="text-right font-normal">规模(亿)</th>
                    <th className="text-right font-normal">T+N</th>
                  </tr>
                </thead>
                <tbody>
                  {meta.map((m) => (
                    <tr key={m.symbol} className="border-b border-neutral-50">
                      <td className="py-1.5 font-mono text-xs">{m.symbol}</td>
                      <td className="font-medium">{m.name ?? '—'}</td>
                      <td className="text-xs text-neutral-600">{m.track_index ?? '—'}</td>
                      <td className="text-xs">
                        {m.fund_type ?? '—'}
                        {m.is_cross_border && <span className="ml-1 rounded bg-purple-50 px-1.5 py-0.5 text-xs text-purple-600">跨境</span>}
                      </td>
                      <td className="text-right tabular-nums text-xs">
                        {m.fund_size != null ? (m.fund_size / 1e8).toFixed(1) : '—'}
                      </td>
                      <td className="text-right tabular-nums text-xs">{m.sellable_after_days ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium">概览</div>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-neutral-400">境内</span><span className="tabular-nums">{local.length}</span></div>
            <div className="flex justify-between"><span className="text-neutral-400">跨境</span><span className="tabular-nums">{cross.length}</span></div>
            <div className="flex justify-between">
              <span className="text-neutral-400">跨境占比</span>
              <span className="tabular-nums">{meta?.length ? `${((cross.length / meta.length) * 100).toFixed(0)}%` : '—'}</span>
            </div>
            <div className="pt-2 text-xs text-neutral-400">「跨境」指 QDII（如纳指/标普），申赎 T+2，不参与 T+0 套利。</div>
          </div>
        </div>
      </div>

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">日收益率相关性（Top）</div>
        {corr?.note ? (
          <div className="py-6 text-center text-sm text-neutral-400">{corr.note}</div>
        ) : !corr?.pairs?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">计算中…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">#</th>
                <th className="text-left font-normal">A</th>
                <th className="text-left font-normal">B</th>
                <th className="text-right font-normal">相关系数</th>
              </tr>
            </thead>
            <tbody>
              {corr.pairs.slice(0, 30).map((p, i) => (
                <tr key={`${p.a}-${p.b}`} className="border-b border-neutral-50">
                  <td className="py-1.5 text-xs text-neutral-400">{i + 1}</td>
                  <td className="font-mono text-xs">{p.a}</td>
                  <td className="font-mono text-xs">{p.b}</td>
                  <td className="text-right tabular-nums text-xs">{p.corr.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}