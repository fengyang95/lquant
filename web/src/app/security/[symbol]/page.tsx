'use client';

import { useParams } from 'next/navigation';
import useSWR from 'swr';
import KChart, { type Overlay } from '@/components/KChart';
import { Pct, fmtNum, fmtYi } from '@/components/QuoteTable';
import { fetcher } from '@/lib/api';

type Quote = {
  symbol: string;
  name?: string | null;
  available: boolean;
  price?: number | null;
  change?: number | null;
  change_pct?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  prev_close?: number | null;
  amount?: number | null;
  market_cap?: number | null;
};

/** /data/indicators 返回行：OHLC + 全套技术指标 */
type IndRow = {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  ma5?: number | null;
  ma10?: number | null;
  ma20?: number | null;
  ma60?: number | null;
  macd_dif?: number | null;
  macd_dea?: number | null;
  macd_hist?: number | null;
  rsi14?: number | null;
  boll_upper?: number | null;
  boll_mid?: number | null;
  boll_lower?: number | null;
};

type FlowRow = {
  trade_date: string;
  main_net_inflow: number | null;
  super_large_net: number | null;
  large_net: number | null;
};

const MA_COLORS: Record<string, string> = {
  ma5: '#ea580c',
  ma10: '#8b5cf6',
  ma20: '#2563eb',
  ma60: '#ca8a04',
};

export default function SecurityPage() {
  const { symbol = '' } = useParams<{ symbol: string }>();

  // 实时行情 5s 轮询；不可用时用日线末根兜底
  const { data: quote } = useSWR<Quote>(
    `/data/quote?symbol=${symbol}`, fetcher, { refreshInterval: 5_000 },
  );
  const { data: rows, isLoading, error } = useSWR<IndRow[]>(
    `/data/indicators?symbol=${symbol}&limit=250`, fetcher,
  );
  const { data: flows } = useSWR<FlowRow[]>(`/market/money-flow?symbol=${symbol}`, fetcher);

  if (isLoading) return <div className="py-20 text-center text-neutral-400">加载中…</div>;
  if (error) return <div className="py-20 text-center text-red-500">加载失败：{String(error)}</div>;

  const last = rows?.[rows.length - 1];
  const prev = rows && rows.length > 1 ? rows[rows.length - 2] : undefined;
  const price = quote?.available && quote.price != null ? quote.price : last?.close;
  const chgPct = quote?.available && quote.change_pct != null
    ? quote.change_pct / 100
    : price && prev?.close ? price / prev.close - 1 : null;
  const name = quote?.name;

  const bars = (rows ?? []).map((r) => ({
    trade_date: r.trade_date, open: r.open, high: r.high,
    low: r.low, close: r.close, volume: r.volume,
  }));
  const overlays: Overlay[] = ['ma5', 'ma20', 'ma60'].map((k) => ({
    name: k,
    color: MA_COLORS[k],
    data: (rows ?? []).map((r) => (r as IndRow & Record<string, number | null>)[k] ?? null),
  }));

  const lastV = (k: keyof IndRow) => (last ? (last[k] as number | null) ?? null : null);

  return (
    <div className="space-y-4">
      {/* 报价头 */}
      <div className="flex flex-wrap items-end justify-between gap-4 rounded-xl border bg-white p-5">
        <div>
          <div className="text-lg font-semibold">
            {name ?? ''}
            <span className="ml-2 font-mono text-sm text-neutral-400">{symbol}</span>
          </div>
          <div className="mt-1 flex items-baseline gap-3">
            <span className={`text-4xl font-semibold tabular-nums ${chgPct != null && chgPct > 0 ? 'text-up' : chgPct != null && chgPct < 0 ? 'text-down' : ''}`}>
              {fmtNum(price)}
            </span>
            <span className="text-lg"><Pct value={chgPct} /></span>
          </div>
          <div className="mt-1 text-xs text-neutral-400">
            {quote?.available
              ? `实时 · 开 ${fmtNum(quote.open)} 高 ${fmtNum(quote.high)} 低 ${fmtNum(quote.low)} 昨收 ${fmtNum(quote.prev_close)}`
              : last
                ? `实时行情不可用，显示 ${last.trade_date} 收盘价`
                : '暂无行情'}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-sm">
          <div className="text-xs text-neutral-400">成交额</div>
          <div className="tabular-nums">{fmtYi(quote?.available ? quote.amount : null)}</div>
          <div className="text-xs text-neutral-400">总市值</div>
          <div className="tabular-nums">{quote?.available ? fmtYi(quote.market_cap) : '—'}</div>
          <div className="text-xs text-neutral-400">数据截至</div>
          <div className="tabular-nums text-neutral-500">{last?.trade_date ?? '—'}</div>
        </div>
      </div>

      {/* 技术指标卡（M3） */}
      <div className="grid grid-cols-2 gap-3 rounded-xl border bg-white p-4 md:grid-cols-4">
        <div>
          <div className="text-xs text-neutral-400">MACD (12,26,9)</div>
          <div className={`font-semibold tabular-nums ${(lastV('macd_hist') ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
            DIF {lastV('macd_dif')?.toFixed(3) ?? '—'} · HIST {lastV('macd_hist')?.toFixed(3) ?? '—'}
          </div>
        </div>
        <div>
          <div className="text-xs text-neutral-400">RSI(14)</div>
          <div className={`font-semibold tabular-nums ${(lastV('rsi14') ?? 50) >= 70 ? 'text-up' : (lastV('rsi14') ?? 50) <= 30 ? 'text-down' : ''}`}>
            {lastV('rsi14')?.toFixed(1) ?? '—'}
            <span className="ml-1 text-xs font-normal text-neutral-400">
              {(lastV('rsi14') ?? 50) >= 70 ? '超买' : (lastV('rsi14') ?? 50) <= 30 ? '超卖' : ''}
            </span>
          </div>
        </div>
        <div>
          <div className="text-xs text-neutral-400">BOLL(20,2)</div>
          <div className="font-semibold tabular-nums text-xs">
            {lastV('boll_lower')?.toFixed(2) ?? '—'} / {lastV('boll_mid')?.toFixed(2) ?? '—'} / {lastV('boll_upper')?.toFixed(2) ?? '—'}
          </div>
        </div>
        <div>
          <div className="text-xs text-neutral-400">均线</div>
          <div className="font-semibold tabular-nums text-xs">
            {['ma5', 'ma20', 'ma60'].map((k) => (
              <span key={k} style={{ color: MA_COLORS[k] }} className="mr-2">
                {lastV(k as keyof IndRow) == null ? '—' : (lastV(k as keyof IndRow) as number).toFixed(2)}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* K 线 + 均线叠加 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex items-center gap-3 text-sm font-medium">
          <span>日 K（近 250 交易日）</span>
          {overlays.map((o) => (
            <span key={o.name} className="flex items-center gap-1 text-xs text-neutral-500">
              <span className="inline-block h-0.5 w-4 rounded" style={{ background: o.color }} />
              {o.name.toUpperCase()}
            </span>
          ))}
        </div>
        <KChart bars={bars} overlays={overlays} />
      </div>

      {/* 资金流 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">主力资金流（近 30 日）</div>
        {!flows?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">
            暂无资金流数据 —— 在数据页采集后可见
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">日期</th>
                <th className="text-right font-normal">主力净流入</th>
                <th className="text-right font-normal">超大单</th>
                <th className="text-right font-normal">大单</th>
              </tr>
            </thead>
            <tbody>
              {flows.map((f) => (
                <tr key={f.trade_date} className="border-b border-neutral-50">
                  <td className="py-1.5 tabular-nums">{f.trade_date}</td>
                  <td className="text-right tabular-nums">
                    <span className={f.main_net_inflow != null && f.main_net_inflow > 0 ? 'text-up' : 'text-down'}>
                      {fmtYi(f.main_net_inflow)}
                    </span>
                  </td>
                  <td className="text-right tabular-nums">{fmtYi(f.super_large_net)}</td>
                  <td className="text-right tabular-nums">{fmtYi(f.large_net)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="text-xs text-neutral-400">
        相关：涨停池/龙虎榜见 <a href="/sectors" className="text-blue-600 hover:underline">板块页</a> ·
        把它加入自选去 <a href="/watchlist" className="text-blue-600 hover:underline">自选页</a>
      </div>
    </div>
  );
}
