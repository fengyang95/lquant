'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import KChart, { type Overlay } from '@/components/KChart';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import IndicatorPicker, {
  IndicatorPickerFallback,
  PRICE_PANE,
  isLineOutput,
  useIndicatorRegistry,
  type IndicatorMeta,
} from '@/components/IndicatorPicker';
import FundamentalCard from '@/components/FundamentalCard';
import SecurityAnalysisView from '@/components/SecurityAnalysis';
import { Pct, fmtNum, fmtYi } from '@/components/QuoteTable';
import { SERIES_COLORS } from '@/lib/chart';
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

/** /data/indicators 返回行：OHLC + 被选中的指标列（列集合由 names 参数决定） */
type IndRow = {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  [key: string]: unknown;
};

type FlowRow = {
  trade_date: string;
  main_net_inflow: number | null;
  super_large_net: number | null;
  large_net: number | null;
};

/** 默认指标集：与迁移前页面一致 */
const DEFAULT_SELECTED = ['ma', 'macd', 'rsi', 'boll'];

const MAX_OVERLAY_LINES = 8;

/** 从选中的指标里挑出可叠加到价格轴上的数值列，并保持稳定顺序 */
function pickOverlayColumns(metas: IndicatorMeta[], selected: string[],
                            rows: IndRow[] | undefined): string[] {
  const cols: string[] = [];
  for (const name of selected) {
    const meta = metas.find((m) => m.name === name);
    // 只看显式声明为 price 面板的指标：MACD 虽属 trend，量纲却与价格差两个数量级
    if (!meta || meta.pane !== PRICE_PANE) continue;
    for (const out of meta.outputs) {
      if (isLineOutput(rows, out)) cols.push(out);
    }
  }
  return cols.slice(0, MAX_OVERLAY_LINES);
}

function fmtCell(v: unknown): string {
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') return v.toFixed(3);
  return '—';
}

export default function SecurityPage() {
  const { symbol = '' } = useParams<{ symbol: string }>();
  const router = useRouter();
  const [selected, setSelected] = useState<string[]>(DEFAULT_SELECTED);

  const { data: registry, error: regErr } = useIndicatorRegistry();
  const names = selected.join(',');

  // 实时行情 5s 轮询；不可用时用日线末根兜底
  const { data: quote } = useSWR<Quote>(
    `/data/quote?symbol=${symbol}`, fetcher, { refreshInterval: 5_000 },
  );
  const { data: rows, isLoading, error } = useSWR<IndRow[]>(
    `/data/indicators?symbol=${symbol}&limit=250&names=${names}`, fetcher,
  );
  const { data: flows } = useSWR<FlowRow[]>(`/market/money-flow?symbol=${symbol}`, fetcher);
  // 多角度分析报告（技术/基本面/估值/资金/相对强度/消息面 + 风险）
  const { data: analysis, isLoading: analysisLoading } = useSWR(
    `/security/${encodeURIComponent(symbol)}/analysis`, fetcher,
  );

  // bars/overlays 仅随 rows（日线）变化；行情 5s 轮询不改 rows 引用，
  // useMemo 保证 KChart 的 effect 不因 quote 更新而重建图表、重置视口
  const bars = useMemo(() => (rows ?? []).map((r) => ({
    trade_date: r.trade_date, open: r.open, high: r.high,
    low: r.low, close: r.close, volume: r.volume,
  })), [rows]);

  const metas = registry?.indicators ?? [];
  const overlayCols = useMemo(
    () => pickOverlayColumns(metas, selected, rows), [metas, selected, rows],
  );
  const overlays: Overlay[] = useMemo(
    () => overlayCols.map((k, i) => ({
      name: k,
      color: SERIES_COLORS[i % SERIES_COLORS.length],
      data: (rows ?? []).map((r) => (typeof r[k] === 'number' ? (r[k] as number) : null)),
    })),
    [overlayCols, rows],
  );

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

  const last = rows?.[rows.length - 1];
  const prev = rows && rows.length > 1 ? rows[rows.length - 2] : undefined;
  const price = quote?.available && quote.price != null ? quote.price : last?.close;
  const chgPct = quote?.available && quote.change_pct != null
    ? quote.change_pct / 100
    : price && prev?.close ? price / prev.close - 1 : null;
  const name = quote?.name;

  const lastV = (k: string) => (last ? (last[k] as number | null) ?? null : null);

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

      {/* 多角度分析：一个代码进，六个角度 + 风险出（本页的核心能力） */}
      <Panel
        title="多角度分析"
        meta="技术 · 基本面 · 估值 · 资金 · 相对强度 · 消息面"
        actions={
          <button className="btn text-xs" onClick={() => router.push('/security')}>
            换一个代码
          </button>
        }
      >
        {analysisLoading
          ? <Loading>分析计算中…</Loading>
          : <SecurityAnalysisView report={analysis} />}
      </Panel>

      {/* 基本面：行业相对分位评分（PIT） */}
      <Panel title="基本面评分" meta="行业相对分位 · PIT">
        <FundamentalCard symbol={symbol} />
      </Panel>

      {/* 技术指标：注册表驱动的选择器 + 分栏指标条 */}
      <Panel
        title="技术指标"
        meta="日线末根"
        actions={
          <span className="text-xs text-ink-faint">已选 {selected.length} 个</span>
        }
      >
        {regErr
          ? <IndicatorPickerFallback error={regErr} />
          : <IndicatorPicker meta={metas} value={selected} onChange={setSelected} />}

        <div className="mt-4 grid grid-cols-1 gap-y-4 divide-line md:grid-cols-3 sm:divide-x">
          {selected.map((n) => {
            const meta = metas.find((m) => m.name === n);
            if (!meta) return null;
            return (
              <div key={n} className="sm:px-4">
                <Stat
                  label={`${meta.label}${meta.min_window ? ` · 预热 ${meta.min_window}` : ''}`}
                  value={
                    <span className="text-base">
                      {meta.outputs.map((o) => (
                        <span key={o} className="mr-2 whitespace-nowrap">
                          <span className="text-xs text-ink-faint">{o}</span>{' '}
                          {fmtCell(last?.[o])}
                        </span>
                      ))}
                    </span>
                  }
                />
              </div>
            );
          })}
        </div>

        {selected.includes('rsi') && (
          <p className="mt-2 text-xs text-ink-faint">
            RSI(14) 当前 {lastV('rsi14')?.toFixed(1) ?? '—'}
            {(lastV('rsi14') ?? 50) >= 70 ? ' · 超买' : (lastV('rsi14') ?? 50) <= 30 ? ' · 超卖' : ''}
          </p>
        )}
      </Panel>

      {/* K 线 + 动态叠加 */}
      <Panel
        title="日 K"
        meta="近 250 交易日"
        actions={
          <div className="flex flex-wrap items-center gap-3">
            {overlays.length === 0 && (
              <span className="text-xs text-ink-faint">当前选中指标无可叠加的价格轴序列</span>
            )}
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
        全市场基本面排名见 <Link href="/fundamental" className="text-indigo hover:underline">基本面页</Link> ·
        把它加入自选去 <Link href="/watchlist" className="text-indigo hover:underline">自选页</Link>
      </div>
    </div>
  );
}
