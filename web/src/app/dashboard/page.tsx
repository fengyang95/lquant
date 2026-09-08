'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import ReactECharts from 'echarts-for-react';
import { get } from '@/lib/api';
import { Pct, fmtNum } from '@/components/QuoteTable';

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

const DEFAULT_POOL = '600519.SH,000001.SZ,601318.SH,510300.SH,159915.SZ,511260.SH';

function Card({ title, extra, children }: { title: string; extra?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-4">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs font-medium text-neutral-500">{title}</div>
        {extra}
      </div>
      {children}
    </div>
  );
}

function Big({ value, unit, color }: { value: number | null | undefined; unit?: string; color?: string }) {
  return (
    <div className={`text-2xl font-semibold tabular-nums ${color ?? ''}`}>
      {value ?? '—'}
      {value != null && unit ? <span className="ml-0.5 text-sm font-normal text-neutral-400">{unit}</span> : null}
    </div>
  );
}

type IndexRow = {
  symbol: string; name: string; close: number; chg: number | null;
  trade_date: string; dates: string[]; closes: number[];
};

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
  const gaugeOption = temp != null ? {
    series: [{
      type: 'gauge', min: 0, max: 100, radius: '95%', center: ['50%', '58%'],
      startAngle: 200, endAngle: -20,
      axisLine: {
        lineStyle: { width: 12, color: [[0.2, '#16a34a'], [0.45, '#86efac'], [0.55, '#fde68a'], [0.8, '#fca5a5'], [1, '#e5484d']] },
      },
      pointer: { length: '55%', width: 4, itemStyle: { color: '#525252' } },
      axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { show: false },
      title: { show: true, offsetCenter: [0, '35%'], fontSize: 11, color: '#737373' },
      detail: {
        valueAnimation: true, offsetCenter: [0, '5%'], fontSize: 26, fontWeight: 600,
        formatter: () => (temp >= 50 ? '亢奋' : temp >= 20 ? '中性' : '冰点'),
      },
      data: [{ value: temp, name: `温度 ${temp}` }],
    }],
  } : null;

  const breadthOption = hist.length ? {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 40, right: 16, top: 28, bottom: 22 },
    xAxis: { type: 'category', data: hist.map((r) => r.trade_date.slice(5)), axisLabel: { fontSize: 10 } },
    yAxis: [
      { type: 'value', axisLabel: { fontSize: 10 } },
      { type: 'value', axisLabel: { fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
    ],
    series: [
      { name: '上涨家数', type: 'bar', stack: 'ud', data: hist.map((r) => r.up), itemStyle: { color: '#e5484d' } },
      { name: '下跌家数', type: 'bar', stack: 'ud', data: hist.map((r) => -r.down), itemStyle: { color: '#16a34a' } },
      { name: '中位涨跌', type: 'line', yAxisIndex: 1, data: hist.map((r) => r.med_chg),
        itemStyle: { color: '#f59e0b' }, showSymbol: false, lineStyle: { width: 1.5 } },
    ],
  } : null;

  const batchOption = batch?.dates?.length ? {
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => v?.toFixed(4) },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 48, right: 16, top: 28, bottom: 22 },
    xAxis: { type: 'category', data: batch.dates.map((d) => d.slice(5)), axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
    series: [
      ...Object.entries(batch.series).map(([sym, vals]) => ({
        name: batch.latest.find((r) => r.symbol === sym)?.name || sym,
        type: 'line', data: vals, showSymbol: false, lineStyle: { width: 1.5 }, connectNulls: true,
      })),
      { name: '等权净值', type: 'line', data: batch.equal_weight_nav, showSymbol: false,
        lineStyle: { width: 2.5, type: 'dashed' }, connectNulls: true, itemStyle: { color: '#111' } },
    ],
  } : null;

  const quickPools = [
    { label: '湖内 ETF', value: '510300.SH,159915.SZ,511260.SH' },
    { label: '白马样本', value: DEFAULT_POOL },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">大盘看板</h1>
        <span className="text-sm text-neutral-400">{b?.trade_date ?? s?.trade_date ?? ''}</span>
      </div>

      {/* 指数行情条（来源 index_daily 采集表，跑一轮收盘采集即有） */}
      {indexQuotes?.length ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          {indexQuotes.map((q) => {
            const base = q.closes[0] ?? 1;
            const spark = q.dates.map((d, i) => [d, +(q.closes[i] / base).toFixed(5)]);
            return (
              <div key={q.symbol} className="rounded-xl border bg-white p-3">
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-medium text-neutral-500">{q.name}</span>
                  <Pct value={q.chg} />
                </div>
                <div className={`text-lg font-semibold tabular-nums ${(q.chg ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                  {q.close?.toFixed(2)}
                </div>
                <ReactECharts
                  option={{
                    grid: { left: 0, right: 0, top: 2, bottom: 0 },
                    xAxis: { type: 'category', show: false, data: spark.map((s) => s[0]) },
                    yAxis: { type: 'value', show: false, min: 'dataMin', max: 'dataMax' },
                    series: [{
                      type: 'line', data: spark.map((s) => s[1]), showSymbol: false,
                      lineStyle: { width: 1.2, color: (q.chg ?? 0) >= 0 ? '#e5484d' : '#16a34a' },
                    }],
                  }}
                  style={{ height: 34 }} notMerge lazyUpdate
                />
              </div>
            );
          })}
        </div>
      ) : null}

      {/* 市场宽度 —— 数据湖日线截面，不依赖采集任务 */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-6">
        <Card title="上涨 / 下跌">
          <div className="flex items-baseline gap-2">
            <Big value={b?.up ?? null} color="text-up" />
            <span className="text-neutral-300">/</span>
            <Big value={b?.down ?? null} color="text-down" />
          </div>
          <div className="mt-1 text-xs text-neutral-400">共 {b?.n ?? '—'} 只（湖内）</div>
        </Card>
        <Card title="上涨占比">
          <Big value={b?.up_ratio == null ? null : +(b.up_ratio * 100).toFixed(1)} unit="%" />
          <div className="mt-2 h-1.5 overflow-hidden rounded bg-neutral-100">
            <div className="h-full bg-red-500" style={{ width: `${(b?.up_ratio ?? 0) * 100}%` }} />
          </div>
        </Card>
        <Card title="涨停 / 跌停（近似）">
          <div className="flex items-baseline gap-2">
            <Big value={b?.limit_up ?? null} color="text-up" />
            <span className="text-neutral-300">/</span>
            <Big value={b?.limit_down ?? null} color="text-down" />
          </div>
          <div className="mt-1 text-xs text-neutral-400">按涨跌幅阈值判定</div>
        </Card>
        <Card title="中位涨跌幅">
          <Pct value={b?.med_chg} />
        </Card>
        <Card title="成交额">
          <Big value={b?.total_amount == null ? null : +(b.total_amount / 1e8).toFixed(0)} unit="亿" />
        </Card>
        <Card title="情绪分 (0-100)">
          <Big value={s?.score ?? null}
               color={s?.score == null ? '' : s.score >= 50 ? 'text-up' : s.score >= 20 ? 'text-flat' : 'text-down'} />
          <div className="mt-1 text-xs text-neutral-400">
            {s?.score == null ? '待采集' : s.score >= 50 ? '亢奋' : s.score >= 20 ? '中性' : '冰点'}
          </div>
        </Card>
      </div>

      {/* 宽度历史 + 市场温度计 */}
      {hist.length ? (
        <div className="grid gap-4 lg:grid-cols-4">
          <div className="lg:col-span-3">
            <Card title="市场宽度 · 近 90 日（上涨/下跌家数 + 中位涨跌幅）">
              <ReactECharts option={breadthOption} style={{ height: 260 }} notMerge />
            </Card>
          </div>
          <Card title="市场温度计">
            {gaugeOption
              ? <ReactECharts option={gaugeOption} style={{ height: 260 }} notMerge />
              : <div className="py-16 text-center text-sm text-neutral-400">暂无宽度数据</div>}
          </Card>
        </div>
      ) : null}

      {/* 批量个股 / ETF 聚合对比 */}
      <Card
        title="批量个股 / ETF 对比（等权净值 + 相对强弱）"
        extra={
          <div className="flex flex-wrap items-center gap-1">
            {quickPools.map((p) => (
              <button key={p.label} onClick={() => applyPool(p.value)}
                className={`rounded-full px-2 py-0.5 text-xs ${pool === p.value ? 'bg-neutral-900 text-white' : 'border text-neutral-500 hover:bg-neutral-100'}`}>
                {p.label}
              </button>
            ))}
          </div>
        }
      >
        <div className="mb-3 flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && applyPool(input)}
            placeholder="逗号分隔代码，如 600519,510300.SH（最多 20 只）"
            className="flex-1 rounded-md border px-3 py-1.5 font-mono text-sm"
          />
          <button
            onClick={() => applyPool(input)}
            className="rounded-md bg-neutral-900 px-4 py-1.5 text-sm text-white hover:bg-neutral-700"
          >
            对比
          </button>
        </div>

        {batch?.summary && (
          <div className="mb-3 grid grid-cols-2 gap-2 text-sm md:grid-cols-5">
            <div><span className="text-xs text-neutral-400">上涨/下跌</span>
              <div className="font-semibold"><span className="text-up">{batch.summary.up}</span> / <span className="text-down">{batch.summary.down}</span></div></div>
            <div><span className="text-xs text-neutral-400">平均涨跌</span><Pct value={batch.summary.avg_chg} /></div>
            <div><span className="text-xs text-neutral-400">最强</span>
              <div className="font-medium">{batch.summary.best?.name || batch.summary.best?.symbol} <Pct value={batch.summary.best?.chg} /></div></div>
            <div><span className="text-xs text-neutral-400">最弱</span>
              <div className="font-medium">{batch.summary.worst?.name || batch.summary.worst?.symbol} <Pct value={batch.summary.worst?.chg} /></div></div>
            <div><span className="text-xs text-neutral-400">等权净值（区间）</span>
              <div className="font-semibold tabular-nums">
                {fmtNum(batch.equal_weight_nav?.[batch.equal_weight_nav.length - 1] ?? null, 4)}
              </div></div>
          </div>
        )}

        {batchOption ? (
          <ReactECharts option={batchOption} style={{ height: 300 }} notMerge />
        ) : (
          <div className="py-6 text-center text-sm text-neutral-400">输入代码后对比（示例：600519,510300.SH）</div>
        )}

        {batch?.latest?.length ? (
          <table className="mt-3 w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">标的</th>
                <th className="text-right font-normal">现价</th>
                <th className="text-right font-normal">涨跌幅</th>
                <th className="text-right font-normal">成交额(亿)</th>
              </tr>
            </thead>
            <tbody>
              {batch.latest.map((r) => (
                <tr key={r.symbol} className="border-b border-neutral-50">
                  <td className="py-1.5">
                    <a href={`/security/${r.symbol}`} className="hover:underline">
                      <span className="font-medium">{r.name || '—'}</span>
                      <span className="ml-1.5 font-mono text-xs text-neutral-400">{r.symbol}</span>
                    </a>
                  </td>
                  <td className="text-right tabular-nums">{fmtNum(r.close)}</td>
                  <td className="text-right"><Pct value={r.chg} /></td>
                  <td className="text-right tabular-nums text-neutral-500">{r.amount_yi}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Card>

      {/* 情绪 / 北向（采集数据，空态降级） */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="情绪历史（采集）">
          {!overview?.sentiment_history?.length ? (
            <div className="py-6 text-center text-sm text-neutral-400">
              暂无 —— 跑 <code className="rounded bg-neutral-100 px-1">POST /api/market/collect</code> 或等待调度
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-neutral-400">
                <tr className="border-b">
                  <th className="py-1.5 text-left font-normal">日期</th>
                  <th className="text-right font-normal">情绪分</th>
                  <th className="text-right font-normal">涨停数</th>
                  <th className="text-right font-normal">炸板率</th>
                </tr>
              </thead>
              <tbody>
                {overview.sentiment_history.slice(0, 10).map((r) => (
                  <tr key={r.trade_date} className="border-b border-neutral-50">
                    <td className="py-1.5 tabular-nums">{r.trade_date}</td>
                    <td className="text-right tabular-nums">{r.sentiment_score}</td>
                    <td className="text-right tabular-nums text-up">{r.limit_up_count}</td>
                    <td className="text-right tabular-nums">{(r.broken_rate * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="北向资金（近 10 日，亿）">
          {!overview?.northbound?.length ? (
            <div className="py-6 text-center text-sm text-neutral-400">暂无数据</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-neutral-400">
                <tr className="border-b">
                  <th className="py-1.5 text-left font-normal">日期</th>
                  <th className="text-right font-normal">沪股通</th>
                  <th className="text-right font-normal">深股通</th>
                  <th className="text-right font-normal">合计</th>
                </tr>
              </thead>
              <tbody>
                {overview.northbound.slice(0, 10).map((r) => (
                  <tr key={r.trade_date} className="border-b border-neutral-50">
                    <td className="py-1.5 tabular-nums">{r.trade_date}</td>
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
        </Card>
      </div>
    </div>
  );
}
