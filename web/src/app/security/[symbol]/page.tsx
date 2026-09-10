'use client';

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import KChart, { type Overlay } from '@/components/KChart';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { Pct, fmtNum, fmtYi } from '@/components/QuoteTable';
import { C } from '@/lib/chart';
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
  ma5: C.gold,
  ma10: '#6B4F9E',
  ma20: C.indigo,
  ma60: C.ink,
};

export default function SecurityPage() {
  const { symbol = '' } = useParams<{ symbol: string }>();
  const router = useRouter();

  // 实时行情 5s 轮询；不可用时用日线末根兜底
  const { data: quote } = useSWR<Quote>(
    `/data/quote?symbol=${symbol}`, fetcher, { refreshInterval: 5_000 },
  );
  const { data: rows, isLoading, error } = useSWR<IndRow[]>(
    `/data/indicators?symbol=${symbol}&limit=250`, fetcher,
  );
  const { data: flows } = useSWR<FlowRow[]>(`/market/money-flow?symbol=${symbol}`, fetcher);

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

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
    <div className="space-y-5">
      <PageHeader
        title={name ? `${name}` : symbol}
        sub={
          <>
            <span className="font-mono">{symbol}</span>
            {quote?.available
              ? ` · 实时 · 开 ${fmtNum(quote.open)} 高 ${fmtNum(quote.high)} 低 ${fmtNum(quote.low)} 昨收 ${fmtNum(quote.prev_close)}`
              : last
                ? ` · 实时行情不可用，显示 ${last.trade_date} 收盘价`
                : ' · 暂无行情'}
          </>
        }
        actions={
          <button className="btn" onClick={() => router.push(`/ask?symbol=${symbol}`)}>
            问 AI
          </button>
        }
      />

      {/* 报价头：宋体大数字 + 关键口径 */}
      <Panel>
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div className="flex items-end gap-4">
            <span className={`font-song text-5xl font-semibold leading-none tabular-nums ${
              chgPct != null && chgPct > 0 ? 'text-up' : chgPct != null && chgPct < 0 ? 'text-down' : 'text-ink'
            }`}>
              {fmtNum(price)}
            </span>
            <span className="pb-1 text-lg"><Pct value={chgPct} /></span>
          </div>
          <div className="grid grid-cols-3 gap-x-10">
            <Stat label="成交额" value={fmtYi(quote?.available ? quote.amount : null)} />
            <Stat label="总市值" value={quote?.available ? fmtYi(quote.market_cap) : '—'} />
            <Stat label="数据截至" value={last?.trade_date ?? '—'} />
          </div>
        </div>
      </Panel>

      {/* 技术指标（M3）：分栏指标条 */}
      <Panel title="技术指标" meta="日线末根">
        <div className="grid grid-cols-2 gap-y-4 divide-line md:grid-cols-4 sm:divide-x">
          <div className="sm:pr-4">
            <Stat label="MACD (12,26,9)"
              value={<span className="text-base">DIF {lastV('macd_dif')?.toFixed(3) ?? '—'} · HIST {lastV('macd_hist')?.toFixed(3) ?? '—'}</span>}
              tone={(lastV('macd_hist') ?? 0) >= 0 ? 'text-up' : 'text-down'} />
          </div>
          <div className="sm:px-4">
            <Stat label="RSI(14)"
              value={<span className="text-base">{lastV('rsi14')?.toFixed(1) ?? '—'}
                <span className="ml-1 font-sans text-xs font-normal text-ink-faint">
                  {(lastV('rsi14') ?? 50) >= 70 ? '超买' : (lastV('rsi14') ?? 50) <= 30 ? '超卖' : ''}
                </span></span>}
              tone={(lastV('rsi14') ?? 50) >= 70 ? 'text-up' : (lastV('rsi14') ?? 50) <= 30 ? 'text-down' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="BOLL(20,2)"
              value={<span className="text-base">{lastV('boll_lower')?.toFixed(2) ?? '—'} / {lastV('boll_mid')?.toFixed(2) ?? '—'} / {lastV('boll_upper')?.toFixed(2) ?? '—'}</span>} />
          </div>
          <div className="sm:px-4">
            <Stat label="均线" value={
              <span className="text-base">
                {['ma5', 'ma20', 'ma60'].map((k) => (
                  <span key={k} style={{ color: MA_COLORS[k] }} className="mr-2">
                    {lastV(k as keyof IndRow) == null ? '—' : (lastV(k as keyof IndRow) as number).toFixed(2)}
                  </span>
                ))}
              </span>
            } />
          </div>
        </div>
      </Panel>

      {/* K 线 + 均线叠加 */}
      <Panel
        title="日 K"
        meta="近 250 交易日"
        actions={
          <div className="flex items-center gap-3">
            {overlays.map((o) => (
              <span key={o.name} className="flex items-center gap-1 text-xs text-ink-dim">
                <span className="inline-block h-0.5 w-4" style={{ background: o.color }} />
                {o.name.toUpperCase()}
              </span>
            ))}
          </div>
        }
      >
        <KChart bars={bars} overlays={overlays} />
      </Panel>

      {/* 资金流 */}
      <Panel title="主力资金流" meta="近 30 日">
        {!flows?.length ? (
          <Empty>暂无资金流数据 —— 在数据页采集后可见</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">日期</th>
                <th className="text-right">主力净流入</th>
                <th className="text-right">超大单</th>
                <th className="text-right">大单</th>
              </tr>
            </thead>
            <tbody>
              {flows.map((f) => (
                <tr key={f.trade_date} className="hover:bg-white">
                  <td className="tabular-nums">{f.trade_date}</td>
                  <td className="text-right">
                    <span className={f.main_net_inflow != null && f.main_net_inflow > 0 ? 'text-up' : 'text-down'}>
                      {fmtYi(f.main_net_inflow)}
                    </span>
                  </td>
                  <td className="text-right">{fmtYi(f.super_large_net)}</td>
                  <td className="text-right">{fmtYi(f.large_net)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <div className="text-xs text-ink-faint">
        相关：涨停池/龙虎榜见 <Link href="/sectors" className="text-indigo hover:underline">板块页</Link> ·
        把它加入自选去 <Link href="/watchlist" className="text-indigo hover:underline">自选页</Link>
      </div>
    </div>
  );
}
