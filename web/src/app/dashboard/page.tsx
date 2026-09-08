'use client';

/**
 * 大盘看板 —— 「研报台」版式：
 * 指数条(竖线分栏) → 宽度截面(分栏指标) → 宽度历史 + 温度计 → 批量对比 → 情绪/北向。
 * 数据逻辑与旧版一致（SWR 轮询、localStorage 池子、温度合成）。
 */

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Pct, fmtNum } from '@/components/QuoteTable';
import { get } from '@/lib/api';
import { C, SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';

type Overview = {
  sentiment: {
    score: number | null; limit_up: number | null; limit_down: number | null;
    broken_rate: number | null; max_consecutive: number | null; trade_date: string | null;
  };
  sentiment_history: { trade_date: string; sentiment_score: number; limit_up_count: number; broken_rate: number }[];
  northbound: { trade_date: string; sh_net_inflow: number; sz_net_inflow: number; total_net_inflow: number }[];
};

type BreadthRow = {
  trade_date: string; n: number; up: number; down: number; flat: number;
  limit_up: number; limit_down: number; med_chg: number | null;
  total_amount: number; up_ratio: number | null;
};

type BatchRow = { symbol: string; name?: string | null; trade_date: string; close: number; chg: number | null; amount_yi: number };
type BatchData = {
  symbols: string[];
  latest: BatchRow[];
  summary: { n: number; up: number; down: number; avg_chg: number | null; best: BatchRow; worst: BatchRow } | null;
  dates: string[];
  series: Record<string, (number | null)[]>;
  equal_weight_nav: (number | null)[];
};

type IndexRow = {
  symbol: string; name: string; close: number; chg: number | null;
  trade_date: string; dates: string[]; closes: number[];
};

const DEFAULT_POOL = '600519.SH,000001.SZ,601318.SH,510300.SH,159915.SZ,511260.SH';

/** 温度计：0~100 合成分 → 分段横条 + 针标 + 状态词 */
function TempScale({ temp }: { temp: number }) {
  const verdict = temp >= 50 ? '亢奋' : temp >= 20 ? '中性' : '冰点';
  const tone = temp >= 55 ? 'text-up' : temp <= 20 ? 'text-down' : 'text-ink';
  // 分段与合成公式对应：冰点 / 转暖 / 中性 / 偏热 / 亢奋
  const segs = [
    { w: 20, c: C.down }, { w: 25, c: '#8FBCA5' }, { w: 10, c: C.inkFaint },
    { w: 25, c: '#D89A93' }, { w: 20, c: C.up },
  ];
  return (
    <div>
      <div className="flex items-end justify-between">
        <span className={`font-song text-[34px] font-semibold leading-none ${tone}`}>{verdict}</span>
        <span className="font-mono text-sm text-ink-dim">温度 {temp}</span>
      </div>
      <div className="relative mt-4 h-2">
        <div className="flex h-full overflow-hidden rounded-[1px]">
          {segs.map((s, i) => <div key={i} style={{ width: `${s.w}%`, background: s.c }} />)}
        </div>
        <div
          className="absolute top-[-4px] h-[16px] w-[2px] bg-ink"
          style={{ left: `calc(${Math.min(Math.max(temp, 0), 100)}% - 1px)` }}
        />
      </div>
      <div className="mt-1.5 flex justify-between font-mono text-[10px] text-ink-faint">
        <span>0 冰点</span><span>50 中性</span><span>100 亢奋</span>
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const { data: overview } = useSWR<Overview>('/market/overview', get, { refreshInterval: 30_000 });
  const { data: breadth } = useSWR<{ latest: BreadthRow | null; history: BreadthRow[] }>(
    '/market/breadth?days=90', get, { refreshInterval: 60_000 });
  const { data: indexQuotes } = useSWR<IndexRow[]>('/market/index?days=60', get, { refreshInterval: 60_000 });

  const [pool, setPool] = useState(DEFAULT_POOL);
  const [input, setInput] = useState(DEFAULT_POOL);
  const { data: batch } = useSWR<BatchData>(
    pool ? `/market/batch?symbols=${encodeURIComponent(pool)}&days=90` : null, get);

  useEffect(() => {
    const saved = localStorage.getItem('lq_dashboard_pool');
    if (saved) { setPool(saved); setInput(saved); }
  }, []);
  function applyPool(v: string) {
    setInput(v);
    setPool(v);
    localStorage.setItem('lq_dashboard_pool', v);
  }

  const b = breadth?.latest;
  const hist = breadth?.history ?? [];
  const s = overview?.sentiment;

  // 市场温度计：上涨占比 + 中位涨跌 + 涨跌停差 合成 0~100
  const temp = b ? Math.max(0, Math.min(100, Math.round(
    (b.up_ratio ?? 0.5) * 60
    + Math.max(-1, Math.min(1, (b.med_chg ?? 0) / 0.03)) * 25
    + Math.max(-1, Math.min(1, ((b.limit_up ?? 0) - (b.limit_down ?? 0)) / 30)) * 15
  ))) : null;

  const breadthOption = hist.length ? {
    tooltip,
    legend: legend({ top: 0 }),
    grid: { left: 44, right: 44, top: 30, bottom: 22 },
    ...axes({ data: hist.map((r) => r.trade_date.slice(5)) }),
    // 中位涨跌挂在右轴 → 需要两个 yAxis
    yAxis: [
      { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      { type: 'value', axisLine: { show: false }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
    ],
    series: [
      { name: '上涨家数', type: 'bar', stack: 'ud', data: hist.map((r) => r.up), itemStyle: { color: C.up } },
      { name: '下跌家数', type: 'bar', stack: 'ud', data: hist.map((r) => -r.down), itemStyle: { color: C.down } },
      { name: '中位涨跌', type: 'line', yAxisIndex: 1, data: hist.map((r) => r.med_chg),
        itemStyle: { color: C.gold }, showSymbol: false, lineStyle: { width: 1.5 } },
    ],
  } : null;

  const batchOption = batch?.dates?.length ? {
    tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(4) },
    legend: legend({ top: 0 }),
    grid: { left: 52, right: 16, top: 30, bottom: 22 },
    ...axes({ data: batch.dates.map((d) => d.slice(5)) }, { scale: true }),
    series: [
      ...Object.entries(batch.series).map(([sym, vals], i) => ({
        name: batch.latest.find((r) => r.symbol === sym)?.name || sym,
        type: 'line', data: vals, showSymbol: false, lineStyle: { width: 1.2, color: SERIES_COLORS[i % SERIES_COLORS.length] },
        itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] }, connectNulls: true,
      })),
      { name: '等权净值', type: 'line', data: batch.equal_weight_nav, showSymbol: false,
        lineStyle: { width: 2.5, type: 'dashed', color: C.ink }, itemStyle: { color: C.ink }, connectNulls: true },
    ],
  } : null;

  const quickPools = [
    { label: '湖内 ETF', value: '510300.SH,159915.SZ,511260.SH' },
    { label: '白马样本', value: DEFAULT_POOL },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="大盘"
        sub={<>数据湖日线截面{b?.trade_date ? ` · ${b.trade_date}` : ''}{s?.trade_date && s.trade_date !== b?.trade_date ? ` / ${s.trade_date}` : ''}</>}
      />

      {/* 指数条：一根发丝线面板，竖线分栏 */}
      {indexQuotes?.length ? (
        <Panel bodyClass="">
          <div className="grid grid-cols-2 divide-line sm:grid-cols-3 sm:divide-x md:grid-cols-6">
            {indexQuotes.map((q) => {
              const base = q.closes[0] ?? 1;
              const spark = q.closes.map((c, i) => +(c / base).toFixed(5));
              return (
                <div key={q.symbol} className="border-b border-line p-3 sm:border-b-0">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-xs text-ink-dim">{q.name}</span>
                    <Pct value={q.chg} />
                  </div>
                  <div className={`font-song text-xl font-semibold tabular-nums leading-snug ${(q.chg ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                    {q.close?.toFixed(2)}
                  </div>
                  <Chart
                    height={34}
                    option={{
                      grid: { left: 0, right: 0, top: 2, bottom: 0 },
                      xAxis: { type: 'category', show: false, data: q.dates },
                      yAxis: { type: 'value', show: false, min: 'dataMin', max: 'dataMax' },
                      series: [{
                        type: 'line', data: spark, showSymbol: false,
                        lineStyle: { width: 1.2, color: (q.chg ?? 0) >= 0 ? C.up : C.down },
                      }],
                    }}
                  />
                </div>
              );
            })}
          </div>
        </Panel>
      ) : null}

      {/* 宽度截面：一行指标，竖线分栏（不拆卡片） */}
      <Panel>
        <div className="grid grid-cols-2 gap-y-4 divide-line sm:grid-cols-3 sm:divide-x lg:grid-cols-6">
          <div className="px-4 first:pl-0">
            <Stat label="上涨 / 下跌" value={<><span className="text-up">{b?.up ?? '—'}</span><span className="mx-1 font-sans text-ink-faint">/</span><span className="text-down">{b?.down ?? '—'}</span></>} hint={`共 ${b?.n ?? '—'} 只（湖内）`} />
          </div>
          <div className="px-4">
            <Stat label="上涨占比" value={b?.up_ratio == null ? '—' : `${(b.up_ratio * 100).toFixed(1)}%`} hint={
              <div className="mt-1 h-1.5 w-full max-w-24 bg-paper">
                <div className="h-full bg-up" style={{ width: `${(b?.up_ratio ?? 0) * 100}%` }} />
              </div>
            } />
          </div>
          <div className="px-4">
            <Stat label="涨停 / 跌停" value={<><span className="text-up">{b?.limit_up ?? '—'}</span><span className="mx-1 font-sans text-ink-faint">/</span><span className="text-down">{b?.limit_down ?? '—'}</span></>} hint="按涨跌幅阈值判定" />
          </div>
          <div className="px-4">
            <Stat label="中位涨跌幅" value={<Pct value={b?.med_chg} />} />
          </div>
          <div className="px-4">
            <Stat label="成交额" value={b?.total_amount == null ? '—' : `${(b.total_amount / 1e8).toFixed(0)} 亿`} />
          </div>
          <div className="px-4">
            <Stat label="情绪分 (0-100)" value={s?.score ?? '—'}
              tone={s?.score == null ? 'text-ink-faint' : s.score >= 50 ? 'text-up' : s.score >= 20 ? 'text-ink' : 'text-down'}
              hint={s?.score == null ? '待采集' : s.score >= 50 ? '亢奋' : s.score >= 20 ? '中性' : '冰点'} />
          </div>
        </div>
      </Panel>

      {/* 宽度历史 + 温度计 */}
      {hist.length ? (
        <div className="grid gap-5 lg:grid-cols-4">
          <div className="lg:col-span-3">
            <Panel title="市场宽度" meta="近 90 日 · 上涨/下跌家数 + 中位涨跌幅">
              <Chart option={breadthOption} height={280} />
            </Panel>
          </div>
          <Panel title="温度计" meta="合成分 0–100">
            {temp != null
              ? <div className="py-6"><TempScale temp={temp} /></div>
              : <div className="py-16 text-center text-sm text-ink-faint">暂无宽度数据</div>}
          </Panel>
        </div>
      ) : null}

      {/* 批量对比 */}
      <Panel
        title="批量对比"
        meta="等权净值 + 相对强弱"
        actions={
          <div className="flex items-center gap-1">
            {quickPools.map((p) => (
              <button key={p.label} onClick={() => applyPool(p.value)}
                className={`tag ${pool === p.value ? 'tag-on' : ''}`}>
                {p.label}
              </button>
            ))}
          </div>
        }
      >
        <div className="mb-4 flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && applyPool(input)}
            placeholder="逗号分隔代码，如 600519,510300.SH（最多 20 只）"
            className="input input-mono flex-1"
          />
          <button onClick={() => applyPool(input)} className="btn btn-primary">对比</button>
        </div>

        {batch?.summary && (
          <div className="mb-4 grid grid-cols-2 gap-y-3 divide-line border-y border-line py-3 text-sm sm:grid-cols-5 sm:divide-x">
            <div className="pr-4">
              <div className="text-xs text-ink-faint">上涨 / 下跌</div>
              <div className="mt-0.5"><span className="text-up">{batch.summary.up}</span> / <span className="text-down">{batch.summary.down}</span></div>
            </div>
            <div className="px-4">
              <div className="text-xs text-ink-faint">平均涨跌</div>
              <div className="mt-0.5"><Pct value={batch.summary.avg_chg} /></div>
            </div>
            <div className="px-4">
              <div className="text-xs text-ink-faint">最强</div>
              <div className="mt-0.5">{batch.summary.best?.name || batch.summary.best?.symbol} <Pct value={batch.summary.best?.chg} /></div>
            </div>
            <div className="px-4">
              <div className="text-xs text-ink-faint">最弱</div>
              <div className="mt-0.5">{batch.summary.worst?.name || batch.summary.worst?.symbol} <Pct value={batch.summary.worst?.chg} /></div>
            </div>
            <div className="px-4">
              <div className="text-xs text-ink-faint">等权净值（区间）</div>
              <div className="mt-0.5 tabular-nums">{fmtNum(batch.equal_weight_nav?.[batch.equal_weight_nav.length - 1] ?? null, 4)}</div>
            </div>
          </div>
        )}

        {batchOption ? (
          <Chart option={batchOption} height={300} />
        ) : (
          <div className="border border-dashed border-line-strong py-8 text-center text-sm text-ink-faint">
            输入代码后对比（示例：600519,510300.SH）
          </div>
        )}

        {batch?.latest?.length ? (
          <table className="table-dense mt-4">
            <thead>
              <tr>
                <th className="text-left">标的</th>
                <th className="text-right">现价</th>
                <th className="text-right">涨跌幅</th>
                <th className="text-right">成交额(亿)</th>
              </tr>
            </thead>
            <tbody>
              {batch.latest.map((r) => (
                <tr key={r.symbol} className="hover:bg-white">
                  <td>
                    <a href={`/security/${r.symbol}`} className="hover:underline">
                      <span className="font-medium">{r.name || '—'}</span>
                      <span className="ml-1.5 font-mono text-xs text-ink-faint">{r.symbol}</span>
                    </a>
                  </td>
                  <td className="text-right">{fmtNum(r.close)}</td>
                  <td className="text-right"><Pct value={r.chg} /></td>
                  <td className="text-right text-ink-dim">{r.amount_yi}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {/* 情绪 / 北向（采集数据，空态降级） */}
      <div className="grid gap-5 md:grid-cols-2">
        <Panel title="情绪历史" meta="采集表">
          {!overview?.sentiment_history?.length ? (
            <div className="border border-dashed border-line-strong py-8 text-center text-sm text-ink-faint">
              暂无 —— 跑 <code className="bg-paper px-1">POST /api/market/collect</code> 或等待调度
            </div>
          ) : (
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="text-left">日期</th>
                  <th className="text-right">情绪分</th>
                  <th className="text-right">涨停数</th>
                  <th className="text-right">炸板率</th>
                </tr>
              </thead>
              <tbody>
                {overview.sentiment_history.slice(0, 10).map((r) => (
                  <tr key={r.trade_date} className="hover:bg-white">
                    <td className="tabular-nums">{r.trade_date}</td>
                    <td className="text-right tabular-nums">{r.sentiment_score}</td>
                    <td className="text-right tabular-nums text-up">{r.limit_up_count}</td>
                    <td className="text-right tabular-nums">{(r.broken_rate * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title="北向资金" meta="近 10 日 · 亿">
          {!overview?.northbound?.length ? (
            <div className="border border-dashed border-line-strong py-8 text-center text-sm text-ink-faint">暂无数据</div>
          ) : (
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="text-left">日期</th>
                  <th className="text-right">沪股通</th>
                  <th className="text-right">深股通</th>
                  <th className="text-right">合计</th>
                </tr>
              </thead>
              <tbody>
                {overview.northbound.slice(0, 10).map((r) => (
                  <tr key={r.trade_date} className="hover:bg-white">
                    <td className="tabular-nums">{r.trade_date}</td>
                    <td className="text-right tabular-nums">{(r.sh_net_inflow / 1e8).toFixed(1)}</td>
                    <td className="text-right tabular-nums">{(r.sz_net_inflow / 1e8).toFixed(1)}</td>
                    <td className={`text-right tabular-nums ${r.total_net_inflow > 0 ? 'text-up' : 'text-down'}`}>
                      {(r.total_net_inflow / 1e8).toFixed(1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}
