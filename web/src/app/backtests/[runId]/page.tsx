'use client';

/**
 * 聚宽风格回测详情页：
 * 头部指标卡行 + 左侧 tab（收益概述/归因分析/每日持仓&收益/单笔进出/性能分析/策略代码）。
 * 数据：/backtests/{id}（净值/月度/滚动/分布/基准/αβ）+ /attribution + /holdings + /code。
 * 颜色约定：A 股习惯 —— 红涨绿跌。
 */

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import ReactECharts from 'echarts-for-react';
import { useParams } from 'next/navigation';
import { get } from '@/lib/api';

const UP = '#e5484d';    // 红涨
const DOWN = '#2ea34b';  // 绿跌
const BLUE = '#2563eb';
const ORANGE = '#f59e0b';
const PURPLE = '#8b5cf6';

type NavPt = { date: string; nav: number; drawdown: number | null };
type Detail = {
  run_id: string; strategy: string; params: Record<string, unknown>; status: string;
  metrics: Record<string, number>;
  nav: NavPt[];
  orders: { ts: string; symbol: string; side: string; qty: number; price: number; fee: number }[];
  monthly: { year: number; month: number; ret: number | null }[];
  rolling: { date: string; vol: number; sharpe: number | null }[];
  return_hist: { lo: number; hi: number; count: number }[];
  benchmark: { date: string; nav: number }[];
  benchmark_label: string;
  risk_vs_benchmark?: {
    alpha_annual?: number; beta?: number; information_ratio?: number | null;
    tracking_error?: number; excess_return?: number; benchmark?: string;
  };
};
type Attribution = {
  stock_contribution: { top: { symbol: string; contribution: number }[]; bottom: { symbol: string; contribution: number }[]; n_stocks: number };
  brinson: { groups: { group: string; alloc: number; select: number; interact: number; total: number }[]; excess_total: number; note: string };
  risk: Record<string, number | string | null>;
};
type HoldingsIdx = { dates: { date: string; nav: number; day_return: number | null }[] };
type HoldingsDay = {
  date: string; nav: number; day_return: number | null; total_value: number;
  positions: { symbol: string; qty: number; close: number; value: number; weight: number }[];
};
type CodeInfo = { run_id: string; code: string | null; benchmark: string | null; engine: string | null };

const pct = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : v.toFixed(digits);
const retColor = (v: number | null | undefined) =>
  v == null ? 'inherit' : v > 0 ? UP : v < 0 ? DOWN : 'inherit';

function Metric({ label, value, tone, hint }: { label: string; value: string; tone?: string; hint?: string }) {
  return (
    <div className="px-3 py-2 min-w-[104px]">
      <div className="text-[11px] text-gray-500 whitespace-nowrap">{label}</div>
      <div className={`text-[15px] font-semibold tabular-nums whitespace-nowrap ${tone ?? ''}`} title={hint}>{value}</div>
    </div>
  );
}

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold text-gray-700">{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}

export default function BacktestDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const [tab, setTab] = useState<'overview' | 'attribution' | 'holdings' | 'trades' | 'perf' | 'code'>('overview');
  const [holdDay, setHoldDay] = useState<string>('');

  const { data: d } = useSWR<Detail>(runId ? `/backtests/${runId}` : null, get);
  const isJq = d?.strategy === 'jq_custom';
  const { data: att } = useSWR<Attribution>(
    runId && tab === 'attribution' ? `/backtests/${runId}/attribution` : null, get);
  const { data: codeInfo } = useSWR<CodeInfo>(
    runId ? `/backtests/${runId}/code` : null, get);
  const { data: holdIdx } = useSWR<HoldingsIdx>(
    runId && tab === 'holdings' ? `/backtests/${runId}/holdings` : null, get);
  const { data: holdDayData } = useSWR<HoldingsDay>(
    runId && tab === 'holdings' && holdDay ? `/backtests/${runId}/holdings?day=${holdDay}` : null, get);

  const m = d?.metrics ?? {};
  const risk = d?.risk_vs_benchmark;

  // 超额净值 = 策略 / 基准（逐日对齐）
  const { excessSeries, benchOption } = useMemo(() => {
    if (!d) return { excessSeries: [] as [string, number][], benchOption: null };
    const bench = new Map(d.benchmark.map((b) => [b.date, b.nav]));
    const dates: string[] = [];
    const strat: number[] = [];
    const benchY: (number | null)[] = [];
    const excess: (number | null)[] = [];
    for (const p of d.nav) {
      const b = bench.get(p.date);
      dates.push(p.date);
      strat.push(+p.nav.toFixed(4));
      benchY.push(b ?? null);
      excess.push(b ? +(p.nav / b - 1).toFixed(4) : null);
    }
    const option = {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略净值', d.benchmark_label, '超额收益'], top: 0 },
      grid: { left: 60, right: 60, top: 30, bottom: 60 },
      xAxis: { type: 'category', data: dates },
      yAxis: [
        { type: 'value', scale: true, axisLabel: { formatter: (v: number) => v.toFixed(2) } },
        { type: 'value', scale: true, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { show: false } },
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        { name: '策略净值', type: 'line', data: strat, showSymbol: false, lineStyle: { width: 1.6, color: BLUE }, itemStyle: { color: BLUE } },
        { name: d.benchmark_label, type: 'line', data: benchY, showSymbol: false, lineStyle: { width: 1.2, color: '#9ca3af' }, itemStyle: { color: '#9ca3af' } },
        { name: '超额收益', type: 'line', yAxisIndex: 1, data: excess, showSymbol: false, lineStyle: { width: 1.2, color: PURPLE }, areaStyle: { color: 'rgba(139,92,246,0.08)' }, itemStyle: { color: PURPLE } },
      ],
    };
    return { excessSeries: excess as unknown as [string, number][], benchOption: option };
  }, [d]);

  const monthlyOption = useMemo(() => {
    if (!d?.monthly?.length) return null;
    const years = [...new Set(d.monthly.map((x) => x.year))].sort();
    const data = d.monthly
      .filter((x) => x.ret != null)
      .map((x) => [String(x.month - 1), String(x.year), +(x.ret! * 100).toFixed(2)]);
    return {
      tooltip: { formatter: (p: { data: [string, string, number] }) => `${p.data[1]}-${+p.data[0] + 1}: ${p.data[2]}%` },
      grid: { left: 70, right: 30, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'] },
      yAxis: { type: 'category', data: years.map(String) },
      visualMap: { min: -10, max: 10, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
        inRange: { color: [DOWN, '#ffffff', UP] }, textStyle: { fontSize: 10 } },
      series: [{ type: 'heatmap', data, label: { show: true, fontSize: 9, formatter: (p: { data: [string, string, number] }) => `${p.data[2]}%` } }],
    };
  }, [d]);

  const histOption = useMemo(() => {
    if (!d?.return_hist?.length) return null;
    return {
      tooltip: {},
      grid: { left: 50, right: 20, top: 10, bottom: 30 },
      xAxis: { type: 'category', data: d.return_hist.map((b) => `${(b.lo * 100).toFixed(1)}%`), axisLabel: { fontSize: 9, rotate: 45 } },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: d.return_hist.map((b) => ({ value: b.count,
        itemStyle: { color: b.lo + b.hi >= 0 ? UP : DOWN } })) }],
    };
  }, [d]);

  const rollingOption = useMemo(() => {
    if (!d?.rolling?.length) return null;
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['20日滚动波动(年化)', '20日滚动夏普'], top: 0 },
      grid: { left: 55, right: 55, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: d.rolling.map((r) => r.date) },
      yAxis: [{ type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
              { type: 'value', splitLine: { show: false } }],
      series: [
        { name: '20日滚动波动(年化)', type: 'line', data: d.rolling.map((r) => +r.vol.toFixed(4)), showSymbol: false, lineStyle: { color: ORANGE, width: 1.2 }, itemStyle: { color: ORANGE } },
        { name: '20日滚动夏普', type: 'line', yAxisIndex: 1, data: d.rolling.map((r) => r.sharpe), showSymbol: false, lineStyle: { color: BLUE, width: 1.2 }, itemStyle: { color: BLUE } },
      ],
    };
  }, [d]);

  const ddOption = useMemo(() => {
    if (!d?.nav?.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 55, right: 20, top: 10, bottom: 30 },
      xAxis: { type: 'category', data: d.nav.map((p) => p.date) },
      yAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: [{ type: 'line', data: d.nav.map((p) => p.drawdown), showSymbol: false,
        lineStyle: { color: DOWN, width: 1 }, areaStyle: { color: 'rgba(46,163,75,0.15)' }, itemStyle: { color: DOWN } }],
    };
  }, [d]);

  const contribOption = useMemo(() => {
    if (!att?.stock_contribution) return null;
    const top = att.stock_contribution.top.slice(0, 12);
    const bot = [...att.stock_contribution.bottom].reverse().slice(0, 12);
    const cats = [...bot.map((x) => x.symbol), ...top.map((x) => x.symbol)];
    const vals = [...bot.map((x) => x.contribution), ...top.map((x) => x.contribution)];
    return {
      tooltip: { valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 90, right: 30, top: 10, bottom: 30 },
      xAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
      yAxis: { type: 'category', data: cats, axisLabel: { fontSize: 10 } },
      series: [{ type: 'bar', data: vals.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) }],
    };
  }, [att]);

  const brinsonOption = useMemo(() => {
    if (!att?.brinson?.groups?.length) return null;
    const gs = att.brinson.groups.slice(0, 15);
    return {
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      legend: { data: ['配置', '选股', '交互'], top: 0 },
      grid: { left: 55, right: 20, top: 30, bottom: 60 },
      xAxis: { type: 'category', data: gs.map((g) => g.group), axisLabel: { rotate: 30, fontSize: 10 } },
      yAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: [
        { name: '配置', type: 'bar', stack: 'b', itemStyle: { color: BLUE }, data: gs.map((g) => +g.alloc.toFixed(4)) },
        { name: '选股', type: 'bar', stack: 'b', itemStyle: { color: UP }, data: gs.map((g) => +g.select.toFixed(4)) },
        { name: '交互', type: 'bar', stack: 'b', itemStyle: { color: ORANGE }, data: gs.map((g) => +g.interact.toFixed(4)) },
      ],
    };
  }, [att]);

  if (!d) {
    return <div className="p-8 text-gray-500">加载中…（run 不存在时会一直为空）</div>;
  }

  const TABS = [
    { id: 'overview', label: '收益概述' },
    { id: 'attribution', label: '归因分析' },
    { id: 'holdings', label: '每日持仓&收益' },
    { id: 'trades', label: '单笔进出' },
    { id: 'perf', label: '性能分析' },
    ...(isJq ? [{ id: 'code' as const, label: '策略代码' }] : []),
  ] as const;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶部条 */}
      <div className="bg-white border-b border-gray-200 px-5 py-3 flex flex-wrap items-center gap-3">
        <div className="text-base font-bold text-gray-800">
          {isJq ? '自定义策略' : String(d.params?.formula ?? d.strategy)}
          {d.params?.top_n ? ` · Top${d.params.top_n}` : ''}
        </div>
        <div className="text-xs text-gray-500">
          {String(d.params?.start ?? d.nav[0]?.date)} 至 {String(d.params?.end ?? d.nav[d.nav.length - 1]?.date)}
          {' · '}¥{num(Number(d.params?.initial_cash ?? m.initial_cash ?? 1_000_000), 0)}
          {' · '}状态 <span className="text-green-600">{d.status === 'done' ? '回测完成' : d.status}</span>
          {' · '}{d.strategy}
        </div>
        <div className="ml-auto flex gap-1">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-3 py-1.5 text-xs rounded-md ${tab === t.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* 指标卡行 */}
      <div className="bg-white border-b border-gray-200 px-3 py-2 flex flex-wrap divide-x divide-gray-100">
        <Metric label="策略收益" value={pct(m.total_return)} tone={retColor(m.total_return)} />
        <Metric label="策略年化" value={pct(m.annual_return)} tone={retColor(m.annual_return)} />
        <Metric label="超额收益" value={pct(risk?.excess_return)} tone={retColor(risk?.excess_return)}
          hint={`基准: ${risk?.benchmark ?? d.benchmark_label}`} />
        <Metric label="基准收益" value={d.benchmark.length > 1
          ? pct(d.benchmark[d.benchmark.length - 1].nav / d.benchmark[0].nav - 1) : '--'} />
        <Metric label="阿尔法 α" value={num(risk?.alpha_annual, 3)} tone={retColor(risk?.alpha_annual)} />
        <Metric label="贝塔 β" value={num(risk?.beta)} />
        <Metric label="夏普比率" value={num(m.sharpe)} />
        <Metric label="索提诺比率" value={num(m.sortino)} />
        <Metric label="信息比率" value={num(risk?.information_ratio ?? undefined)} />
        <Metric label="胜率" value={pct(m.win_rate)} />
        <Metric label="盈亏比" value={num(m.payoff_ratio)} />
        <Metric label="最大回撤" value={pct(m.max_drawdown)} tone={DOWN} />
        <Metric label="波动率" value={pct(m.annual_vol)} />
        <Metric label="换手/费用" value={`¥${num(m.total_fee, 0)}`} hint={`成交 ${m.n_trades} 笔 · 拒单 ${m.n_rejected}`} />
      </div>

      <div className="p-4 space-y-4 max-w-[1400px] mx-auto">
        {tab === 'overview' && (
          <>
            <Card title="净值与超额收益">
              {benchOption && <ReactECharts option={benchOption} style={{ height: 380 }} notMerge />}
            </Card>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {monthlyOption && (
                <Card title="月度收益热力（%）">
                  <ReactECharts option={monthlyOption} style={{ height: 280 }} notMerge />
                </Card>
              )}
              {histOption && (
                <Card title="日收益分布">
                  <ReactECharts option={histOption} style={{ height: 280 }} notMerge />
                </Card>
              )}
            </div>
          </>
        )}

        {tab === 'attribution' && (
          <>
            {!att && <div className="text-gray-500 text-sm">归因计算中…</div>}
            {att && (
              <>
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="px-2 py-1 rounded bg-white border text-gray-600">基准: {String(att.risk.benchmark ?? d.benchmark_label)}</span>
                  <span className="px-2 py-1 rounded bg-white border text-gray-600">跟踪误差: {pct(att.risk.tracking_error as number)}</span>
                  <span className="px-2 py-1 rounded bg-white border text-gray-600">参与个股: {att.stock_contribution.n_stocks}</span>
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                  <Card title="个股收益贡献（正/负 前 12）">
                    {contribOption ? <ReactECharts option={contribOption} style={{ height: 420 }} notMerge /> : <div className="text-gray-400 text-sm">无持仓数据</div>}
                  </Card>
                  <Card title={`分组 Brinson 归因（${att.brinson.note}）`}>
                    {brinsonOption && <ReactECharts option={brinsonOption} style={{ height: 260 }} notMerge />}
                    <table className="w-full text-xs mt-2">
                      <thead><tr className="text-gray-500 border-b">
                        <th className="text-left py-1">分组</th><th className="text-right">配置 α</th>
                        <th className="text-right">选股 α</th><th className="text-right">交互</th><th className="text-right">合计</th>
                      </tr></thead>
                      <tbody>
                        {att.brinson.groups.map((g) => (
                          <tr key={g.group} className="border-b border-gray-50">
                            <td className="py-1">{g.group}</td>
                            <td className="text-right tabular-nums" style={{ color: retColor(g.alloc) }}>{pct(g.alloc)}</td>
                            <td className="text-right tabular-nums" style={{ color: retColor(g.select) }}>{pct(g.select)}</td>
                            <td className="text-right tabular-nums" style={{ color: retColor(g.interact) }}>{pct(g.interact)}</td>
                            <td className="text-right tabular-nums font-medium" style={{ color: retColor(g.total) }}>{pct(g.total)}</td>
                          </tr>
                        ))}
                        <tr className="font-semibold">
                          <td className="py-1">合计</td>
                          <td colSpan={3} />
                          <td className="text-right tabular-nums" style={{ color: retColor(att.brinson.excess_total) }}>
                            {pct(att.brinson.excess_total)}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </Card>
                </div>
              </>
            )}
          </>
        )}

        {tab === 'holdings' && (
          <Card title="每日持仓 & 收益"
            right={holdIdx && (
              <select className="text-xs border rounded px-2 py-1" value={holdDay}
                onChange={(e) => setHoldDay(e.target.value)}>
                <option value="">选择日期…</option>
                {[...holdIdx.dates].reverse().map((x) => (
                  <option key={x.date} value={x.date}>{x.date} {x.day_return != null ? `(${(x.day_return * 100).toFixed(2)}%)` : ''}</option>
                ))}
              </select>
            )}>
            {!holdDay && holdIdx && (
              <div className="overflow-auto max-h-[520px]">
                <table className="w-full text-xs">
                  <thead><tr className="text-gray-500 border-b sticky top-0 bg-white">
                    <th className="text-left py-1">日期</th><th className="text-right">净值</th><th className="text-right">当日收益</th>
                  </tr></thead>
                  <tbody>
                    {holdIdx.dates.map((x) => (
                      <tr key={x.date} className="border-b border-gray-50 hover:bg-blue-50 cursor-pointer"
                        onClick={() => setHoldDay(x.date)}>
                        <td className="py-1">{x.date}</td>
                        <td className="text-right tabular-nums">{num(x.nav, 0)}</td>
                        <td className="text-right tabular-nums" style={{ color: retColor(x.day_return) }}>{pct(x.day_return)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {holdDay && holdDayData && (
              <div>
                <div className="text-xs text-gray-500 mb-2">
                  {holdDayData.date} · 总资产 ¥{num(holdDayData.total_value, 0)} · 当日收益{' '}
                  <span style={{ color: retColor(holdDayData.day_return) }}>{pct(holdDayData.day_return)}</span>
                  <button className="ml-3 text-blue-600" onClick={() => setHoldDay('')}>← 返回列表</button>
                </div>
                <table className="w-full text-xs">
                  <thead><tr className="text-gray-500 border-b">
                    <th className="text-left py-1">代码</th><th className="text-right">持仓量</th>
                    <th className="text-right">收盘价</th><th className="text-right">市值</th><th className="text-right">权重</th>
                  </tr></thead>
                  <tbody>
                    {holdDayData.positions.map((p) => (
                      <tr key={p.symbol} className="border-b border-gray-50">
                        <td className="py-1">{p.symbol}</td>
                        <td className="text-right tabular-nums">{num(p.qty, 0)}</td>
                        <td className="text-right tabular-nums">{num(p.close)}</td>
                        <td className="text-right tabular-nums">¥{num(p.value, 0)}</td>
                        <td className="text-right tabular-nums">{pct(p.weight)}</td>
                      </tr>
                    ))}
                    <tr className="font-medium">
                      <td className="py-1">现金</td><td colSpan={2} />
                      <td className="text-right tabular-nums">
                        ¥{num(holdDayData.total_value - holdDayData.positions.reduce((s, p) => s + p.value, 0), 0)}
                      </td>
                      <td className="text-right tabular-nums">
                        {pct(1 - holdDayData.positions.reduce((s, p) => s + p.weight, 0))}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}

        {tab === 'trades' && (
          <Card title={`单笔进出（${d.orders.length} 笔）`}>
            <div className="overflow-auto max-h-[560px]">
              <table className="w-full text-xs">
                <thead><tr className="text-gray-500 border-b sticky top-0 bg-white">
                  <th className="text-left py-1">日期</th><th className="text-left">代码</th>
                  <th className="text-right">方向</th><th className="text-right">数量</th>
                  <th className="text-right">价格</th><th className="text-right">费用</th><th className="text-right">金额</th>
                </tr></thead>
                <tbody>
                  {d.orders.map((o, i) => (
                    <tr key={i} className="border-b border-gray-50">
                      <td className="py-1">{o.ts.slice(0, 10)}</td>
                      <td>{o.symbol}</td>
                      <td className="text-right" style={{ color: o.side === 'buy' ? UP : DOWN }}>
                        {o.side === 'buy' ? '买入' : '卖出'}
                      </td>
                      <td className="text-right tabular-nums">{num(o.qty, 0)}</td>
                      <td className="text-right tabular-nums">{num(o.price, 3)}</td>
                      <td className="text-right tabular-nums">{num(o.fee, 2)}</td>
                      <td className="text-right tabular-nums">¥{num(o.qty * o.price, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {tab === 'perf' && (
          <>
            <Card title="滚动风险（20 日窗口）">
              {rollingOption ? <ReactECharts option={rollingOption} style={{ height: 280 }} notMerge />
                : <div className="text-gray-400 text-sm">净值不足 20 日</div>}
            </Card>
            <Card title="回撤水下图">
              {ddOption && <ReactECharts option={ddOption} style={{ height: 220 }} notMerge />}
            </Card>
            <Card title="更多指标">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                {[['卡玛比率', num(m.calmar)], ['波动率(年化)', pct(m.annual_vol)],
                  ['偏度', num(m.skew)], ['峰度', num(m.kurtosis)],
                  ['最佳单日', pct(m.best_period)], ['最差单日', pct(m.worst_period)],
                  ['最长水下(日)', num(m.longest_dd_periods, 0)], ['交易笔数', num(m.n_trades, 0)]].map(([k, v]) => (
                  <div key={k} className="rounded border border-gray-100 px-3 py-2 bg-white">
                    <div className="text-gray-500">{k}</div>
                    <div className="font-semibold tabular-nums">{v}</div>
                  </div>
                ))}
              </div>
            </Card>
          </>
        )}

        {tab === 'code' && (
          <Card title={`策略代码${codeInfo?.benchmark ? ` · 基准 ${codeInfo.benchmark}` : ''}${codeInfo?.engine === 'jq_compat' ? ' · 聚宽兼容引擎' : ''}`}>
            <pre className="text-xs bg-gray-900 text-gray-100 rounded-lg p-4 overflow-auto max-h-[600px] leading-5">
              {codeInfo?.code ?? '（非代码策略）'}
            </pre>
          </Card>
        )}
      </div>
    </div>
  );
}
