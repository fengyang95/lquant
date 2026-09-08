'use client';

import Link from 'next/link';

import { fmtNum, fmtPct, fmtYi, pctColorClass } from '@/lib/format';

// A 股约定：涨红跌绿
export function Pct({ value, digits = 2 }: { value?: number | null; digits?: number }) {
  if (value == null || Number.isNaN(value)) return <span className="text-flat">—</span>;
  return <span className={`tabular-nums ${pctColorClass(value)}`}>{fmtPct(value, digits)}</span>;
}

// 兼容旧导入路径（页面里从 QuoteTable 引 fmtNum/fmtYi）
export { fmtNum, fmtYi };

export type QuoteRow = {
  symbol: string;
  name?: string | null;
  close?: number | null;
  change_pct?: number | null;
  amount?: number | null;
  main_net_inflow?: number | null;
  turnover_rate?: number | null;
};

export function SymbolLink({ symbol, name }: { symbol: string; name?: string | null }) {
  return (
    <Link href={`/security/${symbol}`} className="hover:underline">
      <span className="font-medium">{name || '—'}</span>
      <span className="ml-1.5 font-mono text-xs text-neutral-400">{symbol}</span>
    </Link>
  );
}

/** 通用行情表：涨红跌绿、金额自动转亿、代码可点进个股页 */
export default function QuoteTable({
  rows,
  showAmount = true,
  showNetInflow = false,
  empty = '暂无数据',
}: {
  rows: QuoteRow[];
  showAmount?: boolean;
  showNetInflow?: boolean;
  empty?: string;
}) {
  if (!rows?.length) return <div className="py-6 text-center text-sm text-neutral-400">{empty}</div>;
  return (
    <table className="w-full text-sm">
      <thead className="text-xs text-neutral-400">
        <tr className="border-b">
          <th className="py-1.5 text-left font-normal">标的</th>
          <th className="text-right font-normal">现价</th>
          <th className="text-right font-normal">涨跌幅</th>
          {showAmount && <th className="text-right font-normal">成交额</th>}
          {showNetInflow && <th className="text-right font-normal">主力净流入</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol} className="border-b border-neutral-50 hover:bg-neutral-50/60">
            <td className="py-1.5"><SymbolLink symbol={r.symbol} name={r.name} /></td>
            <td className="text-right tabular-nums">{fmtNum(r.close)}</td>
            <td className="text-right"><Pct value={r.change_pct} /></td>
            {showAmount && <td className="text-right tabular-nums text-neutral-500">{fmtYi(r.amount)}</td>}
            {showNetInflow && (
              <td className="text-right tabular-nums">
                {r.main_net_inflow == null ? '—' : (
                  <span className={r.main_net_inflow > 0 ? 'text-up' : r.main_net_inflow < 0 ? 'text-down' : ''}>
                    {fmtYi(r.main_net_inflow)}
                  </span>
                )}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
