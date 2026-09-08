'use client';

import Link from 'next/link';

import { fmtNum, fmtPct, fmtYi, pctColorClass } from '@/lib/format';

// A 股约定：涨红跌绿（色板见 tailwind.config：up 朱砂 / down 青绿）
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
      <span className="ml-1.5 font-mono text-xs text-ink-faint">{symbol}</span>
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
  if (!rows?.length) return <div className="py-6 text-center text-sm text-ink-faint">{empty}</div>;
  return (
    <table className="table-dense">
      <thead>
        <tr>
          <th className="text-left">标的</th>
          <th className="text-right">现价</th>
          <th className="text-right">涨跌幅</th>
          {showAmount && <th className="text-right">成交额</th>}
          {showNetInflow && <th className="text-right">主力净流入</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol} className="hover:bg-white">
            <td className="py-2"><SymbolLink symbol={r.symbol} name={r.name} /></td>
            <td className="text-right">{fmtNum(r.close)}</td>
            <td className="text-right"><Pct value={r.change_pct} /></td>
            {showAmount && <td className="text-right text-ink-dim">{fmtYi(r.amount)}</td>}
            {showNetInflow && (
              <td className="text-right">
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
