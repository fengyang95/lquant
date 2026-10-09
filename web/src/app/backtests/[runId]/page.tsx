'use client';

/**
 * 回测详情页（聚宽风格信息架构）—— 「研报台」版式：
 * 页头（宋体标题 + tab 按钮）→ 指标分栏条 → 各 tab 内容。
 * 数据：/backtests/{id}（净值/月度/滚动/分布/基准/αβ）+ /attribution + /holdings + /code。
 * 颜色约定：A 股习惯 —— 红涨绿跌（C.up 朱砂 / C.down 青绿）。
 */

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { ErrorNote, Loading } from '@/components/States';
import { get } from '@/lib/api';
import { C, SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';
import {
  customChartOption,
  partitionCustomAnalysis,
  recordChartOption,
  type CustomRawItem,
} from '@/lib/backtestDetail';

type NavPt = { date: string; nav: number; drawdown: number | null };
type RecordPt = { date: string; value: number };
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
  records?: { [key: string]: RecordPt[] };
  logs?: string[];
  custom_analysis?: CustomRawItem[];
};
type BrinsonGroups = { group: string; alloc: number; select: number; interact: number; total: number }[];
type Attribution = {
  stock_contribution: { top: { symbol: string; contribution: number }[]; bottom: { symbol: string; contribution: number }[]; n_stocks: number };
  residual_by_day: { date: string; residual: number; return: number }[];
  brinson: { groups: BrinsonGroups; excess_total: number; note: string };
  brinson_monthly: { months: { month: string; groups: BrinsonGroups; excess_total: number }[]; note: string };
  cost: { fee_by_day: { date: string; fee: number; drag: number }[]; total_fee: number; total_drag: number };
  profile: {
    industry: { dates: string[]; series: Record<string, number[]> };
    style: { dates: string[]; series: Record<string, number[]> };
    concentration: { date: string; hhi: number; top5: number; top10: number; n_pos: number }[];
    note: string;
  };
  style_attr: {
    dates: string[]; factors: string[];
    factor_cum: Record<string, number[]>;
    common_cum: number[]; specific_cum: number[];
    totals: Record<string, number>;
    note: string;
  };
  drawdown: {
    periods: {
      start: string; trough: string; end: string | null; recovered: boolean;
      drawdown: number; days: number; ret: number;
      stock_top: { symbol: string; contribution: number }[];
      stock_bottom: { symbol: string; contribution: number }[];
      common?: number; specific?: number; factors?: Record<string, number>;
      n_dec?: number;
    }[];
    note: string;
  };
  risk_attr: {
    n_days?: number;
    vol_total?: number; vol_common?: number; vol_specific?: number;
    var_total?: number; var_common?: number; var_specific?: number;
    cross_term?: number; systematic_var_barra?: number;
    factors?: { factor: string; var_contrib: number; pct: number | null; avg_exposure: number }[];
    note: string;
  };
  risk: Record<string, number | string | null>;
};
type HoldingsIdx = { dates: { date: string; nav: number; day_return: number | null }[] };
type HoldingsDay = {
  date: string; nav: number; day_return: number | null; total_value: number;
  positions: { symbol: string; qty: number; close: number; value: number; weight: number }[];
};
type CodeInfo = { run_id: string; code: string | null; benchmark: string | null; engine: string | null };

type TabId = 'overview' | 'attribution' | 'holdings' | 'trades' | 'perf' | 'code';

const pct = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : v.toFixed(digits);
/** 涨红跌绿的文字色类（无值时中性） */
const retCls = (v: number | null | undefined) =>
  v == null ? '' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : '';

export default function BacktestDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const [tab, setTab] = useState<TabId>('overview');
  const [holdDay, setHoldDay] = useState<string>('');
  const [brinsonMonth, setBrinsonMonth] = useState<string>('');

  // 主数据必须带错误分支：404/500 时给出明确提示，而不是永远"加载中"
  const { data: d, error: mainError } = useSWR<Detail>(runId ? `/backtests/${runId}` : null, get);
  const isJq = d?.strategy === 'jq_custom';
  const { data: att } = useSWR<Attribution>(
    runId && tab === 'attribution' ? `/backtests/${runId}/attribution` : null, get);
  const { data: codeInfo } = useSWR<CodeInfo>(
    runId ? `/backtests/${runId}/code` : null, get);
  const { data: holdIdx } = useSWR<HoldingsIdx>(
    runId && tab === 'holdings' ? `/backtests/${runId}/holdings` : null, get);
  const { data: holdDayData, isLoading: holdDayLoading, error: holdDayError } = useSWR<HoldingsDay>(
    runId && tab === 'holdings' && holdDay ? `/backtests/${runId}/holdings?day=${holdDay}` : null, get);

  const m = d?.metrics ?? {};
  const risk = d?.risk_vs_benchmark;

  // 超额净值 = 策略 / 基准（逐日对齐）；benchmark 缺失时仅画策略净值
  const benchOption = useMemo(() => {
    if (!d?.nav?.length) return null;
    const bench = new Map((d.benchmark ?? []).map((b) => [b.date, b.nav]));
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
    return {
      tooltip,
      legend: legend({ data: ['策略净值', d.benchmark_label, '超额收益'], top: 0 }),
      grid: { left: 60, right: 60, top: 30, bottom: 60 },
      xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: [
        { type: 'value', scale: true, axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => v.toFixed(2) } },
        { type: 'value', scale: true, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        { name: '策略净值', type: 'line', data: strat, showSymbol: false, lineStyle: { width: 1.6, color: C.indigo }, itemStyle: { color: C.indigo } },
        { name: d.benchmark_label, type: 'line', data: benchY, showSymbol: false, lineStyle: { width: 1.2, color: C.inkFaint }, itemStyle: { color: C.inkFaint } },
        { name: '超额收益', type: 'line', yAxisIndex: 1, data: excess, showSymbol: false, lineStyle: { width: 1.2, color: C.gold }, areaStyle: { color: 'rgba(176,138,62,0.08)' }, itemStyle: { color: C.gold } },
      ],
    };
  }, [d]);

  const monthlyOption = useMemo(() => {
    if (!d?.monthly?.length) return null;
    const years = [...new Set(d.monthly.map((x) => x.year))].sort();
    const data = d.monthly
      .filter((x): x is typeof x & { ret: number } => x.ret != null)
      .map((x) => [String(x.month - 1), String(x.year), +(x.ret * 100).toFixed(2)]);
    return {
      tooltip: {
        ...tooltip, trigger: 'item',
        formatter: (p: { data: [string, string, number] }) => `${p.data[1]}-${+p.data[0] + 1}: ${p.data[2]}%`,
      },
      grid: { left: 70, right: 30, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'], axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: { type: 'category', data: years.map(String), axisLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      visualMap: { min: -10, max: 10, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
        inRange: { color: [C.down, '#FFFFFF', C.up] }, textStyle: { fontSize: 10, color: C.inkDim } },
      series: [{ type: 'heatmap', data, label: { show: true, fontSize: 9, formatter: (p: { data: [string, string, number] }) => `${p.data[2]}%` } }],
    };
  }, [d]);

  const histOption = useMemo(() => {
    if (!d?.return_hist?.length) return null;
    return {
      tooltip: { ...tooltip, trigger: 'item' },
      grid: { left: 50, right: 20, top: 10, bottom: 30 },
      ...axes({ data: d.return_hist.map((b) => `${(b.lo * 100).toFixed(1)}%`), axisLabel: { color: C.inkDim, fontSize: 9, rotate: 45 } }),
      series: [{ type: 'bar', data: d.return_hist.map((b) => ({ value: b.count,
        itemStyle: { color: b.lo + b.hi >= 0 ? C.up : C.down } })) }],
    };
  }, [d]);

  const rollingOption = useMemo(() => {
    if (!d?.rolling?.length) return null;
    return {
      tooltip,
      legend: legend({ data: ['20日滚动波动(年化)', '20日滚动夏普'], top: 0 }),
      grid: { left: 55, right: 55, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: d.rolling.map((r) => r.date), axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: [
        { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
        { type: 'value', axisLine: { show: false }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      ],
      series: [
        { name: '20日滚动波动(年化)', type: 'line', data: d.rolling.map((r) => +r.vol.toFixed(4)), showSymbol: false, lineStyle: { color: C.gold, width: 1.2 }, itemStyle: { color: C.gold } },
        { name: '20日滚动夏普', type: 'line', yAxisIndex: 1, data: d.rolling.map((r) => r.sharpe), showSymbol: false, lineStyle: { color: C.indigo, width: 1.2 }, itemStyle: { color: C.indigo } },
      ],
    };
  }, [d]);

  const ddOption = useMemo(() => {
    if (!d?.nav?.length) return null;
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 55, right: 20, top: 10, bottom: 30 },
      ...axes({ data: d.nav.map((p) => p.date) }, { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } }),
      series: [{ type: 'line', data: d.nav.map((p) => p.drawdown), showSymbol: false,
        lineStyle: { color: C.down, width: 1 }, areaStyle: { color: 'rgba(30,124,85,0.12)' }, itemStyle: { color: C.down } }],
    };
  }, [d]);

  const contribOption = useMemo(() => {
    if (!att?.stock_contribution) return null;
    const top = att.stock_contribution.top.slice(0, 12);
    const bot = [...att.stock_contribution.bottom].reverse().slice(0, 12);
    const cats = [...bot.map((x) => x.symbol), ...top.map((x) => x.symbol)];
    const vals = [...bot.map((x) => x.contribution), ...top.map((x) => x.contribution)];
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 90, right: 30, top: 10, bottom: 30 },
      xAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
      yAxis: { type: 'category', data: cats, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      series: [{ type: 'bar', data: vals.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? C.up : C.down } })) }],
    };
  }, [att]);

  // Brinson 图表（全区间 or 选定月份，与表格共用数据）
  const activeBrinson = useMemo(() => {
    if (!att?.brinson) return null;
    if (!brinsonMonth || !att.brinson_monthly?.months?.length) return att.brinson;
    const m = att.brinson_monthly.months.find((x) => x.month === brinsonMonth);
    return m ? { groups: m.groups, excess_total: m.excess_total, note: `${m.month} · ${att.brinson_monthly.note}` }
             : att.brinson;
  }, [att, brinsonMonth]);

  const brinsonView = useMemo(() => {
    if (!activeBrinson?.groups?.length) return null;
    const gs = activeBrinson.groups.slice(0, 15);
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      legend: legend({ data: ['配置', '选股', '交互'], top: 0 }),
      grid: { left: 55, right: 20, top: 30, bottom: 60 },
      ...axes(
        { data: gs.map((g) => g.group), axisLabel: { color: C.inkDim, fontSize: 10, rotate: 30 } },
        { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      ),
      series: [
        { name: '配置', type: 'bar', stack: 'b', itemStyle: { color: C.indigo }, data: gs.map((g) => +g.alloc.toFixed(4)) },
        { name: '选股', type: 'bar', stack: 'b', itemStyle: { color: C.up }, data: gs.map((g) => +g.select.toFixed(4)) },
        { name: '交互', type: 'bar', stack: 'b', itemStyle: { color: C.gold }, data: gs.map((g) => +g.interact.toFixed(4)) },
      ],
    };
  }, [activeBrinson]);

  // 择时残差：每日收益中个股解释不掉的部分（费用/滑点/开盘成交择时）
  const residualOption = useMemo(() => {
    if (!att?.residual_by_day?.length) return null;
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(3)}%` },
      grid: { left: 55, right: 20, top: 10, bottom: 30 },
      ...axes(
        { data: att.residual_by_day.map((r) => r.date) },
        { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
      ),
      series: [{ type: 'bar', data: att.residual_by_day.map((r) => ({
        value: r.residual,
        itemStyle: { color: r.residual >= 0 ? C.up : C.down },
      })) }],
    };
  }, [att]);

  // 成本拖累：费用/前日净值的逐日累计（残差里费用部分的显式拆出）
  const costOption = useMemo(() => {
    if (!att?.cost?.fee_by_day?.length) return null;
    let acc = 0;
    const cum = att.cost.fee_by_day.map((r) => { acc += r.drag; return +acc.toFixed(6); });
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(3)}%` },
      grid: { left: 55, right: 20, top: 10, bottom: 30 },
      ...axes(
        { data: att.cost.fee_by_day.map((r) => r.date) },
        { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
      ),
      series: [{ type: 'line', data: cum, showSymbol: false,
        lineStyle: { color: C.down, width: 1.4 }, areaStyle: { color: 'rgba(30,124,85,0.10)' }, itemStyle: { color: C.down } }],
    };
  }, [att]);

  // 行业暴露时序：按平均权重取前 8，堆叠面积（占比口径）
  const industryOption = useMemo(() => {
    const ind = att?.profile?.industry;
    if (!ind?.dates?.length || !Object.keys(ind.series).length) return null;
    const keys = Object.keys(ind.series).slice(0, 8);
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(1)}%` },
      legend: legend({ data: keys, top: 0, type: 'scroll' }),
      grid: { left: 50, right: 20, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: ind.dates, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: { type: 'value', max: 1, axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: keys.map((k, i) => ({
        name: k, type: 'line', stack: 'ind', showSymbol: false,
        lineStyle: { width: 0.6, color: SERIES_COLORS[i % SERIES_COLORS.length] },
        areaStyle: { opacity: 0.55, color: SERIES_COLORS[i % SERIES_COLORS.length] },
        itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
        data: ind.series[k],
      })),
    };
  }, [att]);

  // 集中度：Top5/Top10 权重 + HHI
  const concOption = useMemo(() => {
    const c = att?.profile?.concentration;
    if (!c?.length) return null;
    return {
      tooltip,
      legend: legend({ data: ['Top5 权重', 'Top10 权重', 'HHI'], top: 0 }),
      grid: { left: 50, right: 20, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: c.map((x) => x.date), axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: [
        { name: 'Top5 权重', type: 'line', data: c.map((x) => x.top5), showSymbol: false, lineStyle: { color: C.indigo, width: 1.2 }, itemStyle: { color: C.indigo } },
        { name: 'Top10 权重', type: 'line', data: c.map((x) => x.top10), showSymbol: false, lineStyle: { color: C.gold, width: 1.2 }, itemStyle: { color: C.gold } },
        { name: 'HHI', type: 'line', data: c.map((x) => x.hhi), showSymbol: false, lineStyle: { color: C.inkFaint, width: 1, type: 'dashed' }, itemStyle: { color: C.inkFaint } },
      ],
    };
  }, [att]);

  // 风格暴露漂移：时序 z 分数标准化（量纲不同，只看漂移方向与幅度）
  const styleExposureOption = useMemo(() => {
    const st = att?.profile?.style;
    if (!st?.dates?.length || !Object.keys(st.series).length) return null;
    const keys = Object.keys(st.series);
    const zOf = (vals: number[]) => {
      const mean = vals.reduce((s, v) => s + v, 0) / vals.length;
      const sd = Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / vals.length);
      return sd > 1e-12 ? vals.map((v) => +((v - mean) / sd).toFixed(4)) : vals.map(() => 0);
    };
    return {
      tooltip,
      legend: legend({ data: keys, top: 0, type: 'scroll' }),
      grid: { left: 45, right: 20, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: st.dates, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      series: keys.map((k, i) => ({
        name: k, type: 'line', showSymbol: false,
        lineStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length], width: 1.2 },
        itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
        data: zOf(st.series[k]),
      })),
    };
  }, [att]);

  // 风格收益归因：逐日截面回归的因子累计贡献 vs 特异收益（alpha）
  const styleAttrOption = useMemo(() => {
    const sa = att?.style_attr;
    if (!sa?.dates?.length) return null;
    const names = [...sa.factors, '特异收益(Alpha)'];
    const series = sa.factors.map((k, i) => ({
      name: k, type: 'line', showSymbol: false,
      lineStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length], width: 1.2 },
      itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
      data: sa.factor_cum[k],
    }));
    series.push({
      name: '特异收益(Alpha)', type: 'line', showSymbol: false,
      lineStyle: { color: C.up, width: 1.8 }, itemStyle: { color: C.up },
      data: sa.specific_cum,
    });
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      legend: legend({ data: names, top: 0, type: 'scroll' }),
      grid: { left: 55, right: 20, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: sa.dates, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
      series,
    };
  }, [att]);

  if (mainError) {
    return (
      <div className="space-y-3">
        <ErrorNote>
          {mainError instanceof Error
            ? mainError.message
            : `加载运行 ${runId} 失败（不存在或服务异常）`}
        </ErrorNote>
        <Link href="/backtests" className="btn btn-sm">← 返回回测列表</Link>
      </div>
    );
  }
  if (!d) {
    return <Loading>加载中…</Loading>;
  }

  const TABS: { id: TabId; label: string }[] = [
    { id: 'overview', label: '收益概述' },
    { id: 'attribution', label: '归因分析' },
    { id: 'holdings', label: '每日持仓&收益' },
    { id: 'trades', label: '单笔进出' },
    { id: 'perf', label: '性能分析' },
    ...(isJq ? [{ id: 'code' as const, label: '策略代码' }] : []),
  ];

  const METRICS: { label: string; value: string; tone?: string; hint?: string }[] = [
    { label: '策略收益', value: pct(m.total_return), tone: retCls(m.total_return) },
    { label: '策略年化', value: pct(m.annual_return), tone: retCls(m.annual_return) },
    { label: '超额收益', value: pct(risk?.excess_return), tone: retCls(risk?.excess_return),
      hint: `基准 ${risk?.benchmark ?? d.benchmark_label}` },
    { label: '基准收益', value: (d.benchmark?.length ?? 0) > 1
      ? pct(d.benchmark[d.benchmark.length - 1].nav / d.benchmark[0].nav - 1) : '--' },
    { label: '阿尔法 α', value: num(risk?.alpha_annual, 3), tone: retCls(risk?.alpha_annual) },
    { label: '贝塔 β', value: num(risk?.beta) },
    { label: '夏普比率', value: num(m.sharpe) },
    { label: '索提诺比率', value: num(m.sortino) },
    { label: '信息比率', value: num(risk?.information_ratio ?? undefined) },
    { label: '胜率', value: pct(m.win_rate) },
    { label: '盈亏比', value: num(m.payoff_ratio) },
    { label: '最大回撤', value: pct(m.max_drawdown), tone: 'text-down' },
    { label: '波动率', value: pct(m.annual_vol) },
    { label: '换手/费用', value: `¥${num(m.total_fee, 0)}`,
      hint: `成交 ${m.n_trades} 笔 · 拒单 ${m.n_rejected}` },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title={isJq ? '自定义策略' : String(d.params?.formula ?? d.strategy)}
        sub={
          <>
            {String(d.params?.start ?? d.nav[0]?.date ?? '—')} 至 {String(d.params?.end ?? d.nav[d.nav.length - 1]?.date ?? '—')}
            {' · '}¥{num(Number(d.params?.initial_cash ?? m.initial_cash ?? 1_000_000), 0)}
            {' · '}状态 <span className={d.status === 'done' ? 'text-down' : ''}>{d.status === 'done' ? '回测完成' : d.status}</span>
            {' · '}{d.strategy}
          </>
        }
        actions={TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`btn btn-sm ${tab === t.id ? 'btn-primary' : ''}`}>
            {t.label}
          </button>
        ))}
      />

      {/* 指标分栏条 */}
      <Panel bodyClass="p-0">
        <div className="grid grid-cols-2 divide-line sm:grid-cols-4 lg:grid-cols-7 sm:divide-x">
          {METRICS.map((k) => (
            <div key={k.label} className="border-b border-line px-4 py-3 sm:border-b-0">
              <Stat label={k.label} value={k.value} tone={k.tone} hint={k.hint} />
            </div>
          ))}
        </div>
      </Panel>

      {tab === 'overview' && (
        <>
          <Panel title="净值与超额收益">
            {benchOption && <Chart option={benchOption} height={380} />}
          </Panel>
          {Object.entries(d.records ?? {}).map(([key, pts]) => {
            const opt = recordChartOption(key, pts);
            return opt ? (
              <Panel key={key} title={`自定义曲线 · ${key}`}>
                <Chart option={opt} height={200} />
              </Panel>
            ) : null;
          })}
          {(d.custom_analysis?.length ?? 0) > 0 && (() => {
            const { valid, errors } = partitionCustomAnalysis(d.custom_analysis);
            return (
              <Panel title="自定义分析">
                <div className="space-y-5">
                  {valid.map((item, i) =>
                    item.type === 'chart' ? (
                      (() => {
                        const opt = customChartOption(item);
                        return opt ? (
                          <div key={i}>
                            {item.title && <div className="mb-1 text-xs text-ink-dim">{item.title}</div>}
                            <Chart option={opt} height={240} />
                          </div>
                        ) : null;
                      })()
                    ) : item.columns?.length ? (
                      <div key={i}>
                        {item.title && <div className="mb-1 text-xs text-ink-dim">{item.title}</div>}
                        <table className="table-dense text-xs">
                          <thead>
                            <tr>{item.columns.map((c) => <th key={c} className="text-left">{c}</th>)}</tr>
                          </thead>
                          <tbody>
                            {item.rows.map((row, ri) => (
                              <tr key={ri} className="hover:bg-white">
                                {row.map((cell, ci) => <td key={ci}>{String(cell ?? '--')}</td>)}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : null,
                  )}
                  {errors.map((e, i) => (
                    <div
                      key={`err-${i}`}
                      className="rounded-[2px] border border-line bg-panel px-3 py-2 text-xs text-gold"
                    >
                      <span className="font-medium">⚠ {e.name ?? '自定义分析'}</span>
                      <span className="ml-2 text-ink-dim">{e.error}</span>
                    </div>
                  ))}
                </div>
              </Panel>
            );
          })()}
          {(d.logs?.length ?? 0) > 0 && (
            <details className="rounded-[2px] border border-line bg-panel px-4 py-3">
              <summary className="cursor-pointer text-sm font-medium text-ink-dim">
                运行日志（{(d.logs ?? []).length} 条）
              </summary>
              <pre className="mt-2 max-h-[320px] overflow-auto text-xs leading-5 text-ink-dim">
                {(d.logs ?? []).join('\n')}
              </pre>
            </details>
          )}
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            {monthlyOption && (
              <Panel title="月度收益热力（%）">
                <Chart option={monthlyOption} height={280} />
              </Panel>
            )}
            {histOption && (
              <Panel title="日收益分布">
                <Chart option={histOption} height={280} />
              </Panel>
            )}
          </div>
        </>
      )}

      {tab === 'attribution' && (
        <>
          {!att && <Loading>归因计算中…</Loading>}
          {att && (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="tag">基准 {String(att.risk.benchmark ?? d.benchmark_label)}</span>
                <span className="tag">跟踪误差 {pct(typeof att.risk.tracking_error === 'number' ? att.risk.tracking_error : null)}</span>
                <span className="tag">参与个股 {att.stock_contribution.n_stocks}</span>
                {att.cost && (
                  <span className="tag">费用拖累 <span className="text-down">{pct(att.cost.total_drag)}</span>（¥{num(att.cost.total_fee, 0)}）</span>
                )}
                {att.style_attr?.totals && (
                  <span className="tag">特异 α <span className={retCls(att.style_attr.totals.specific)}>{pct(att.style_attr.totals.specific)}</span></span>
                )}
                {att.risk_attr?.vol_total != null && (
                  <span className="tag">年化波动 {pct(att.risk_attr.vol_total)}（系统性 {pct(att.risk_attr.vol_common)} / 特异 {pct(att.risk_attr.vol_specific)}）</span>
                )}
                <a
                  className="tag text-indigo hover:underline"
                  href={`/api/backtests/${runId}/attribution/report`}
                  target="_blank" rel="noreferrer"
                >
                  导出 HTML 报告 ↗
                </a>
              </div>
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                <Panel title="个股收益贡献" meta="正 / 负 前 12">
                  {contribOption ? <Chart option={contribOption} height={420} /> : <div className="text-sm text-ink-faint">无持仓数据</div>}
                </Panel>
                <Panel
                  title="分组 Brinson 归因"
                  meta={activeBrinson?.note}
                  actions={att.brinson_monthly?.months?.length ? (
                    <select className="input text-xs" value={brinsonMonth}
                      onChange={(e) => setBrinsonMonth(e.target.value)}>
                      <option value="">全区间</option>
                      {[...att.brinson_monthly.months].reverse().map((m) => (
                        <option key={m.month} value={m.month}>{m.month}</option>
                      ))}
                    </select>
                  ) : undefined}
                >
                  {brinsonView && <Chart option={brinsonView} height={260} />}
                  <table className="table-dense mt-3 text-xs">
                    <thead>
                      <tr>
                        <th className="text-left">分组</th><th className="text-right">配置 α</th>
                        <th className="text-right">选股 α</th><th className="text-right">交互</th><th className="text-right">合计</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(activeBrinson?.groups ?? []).map((g) => (
                        <tr key={g.group} className="hover:bg-white">
                          <td>{g.group}</td>
                          <td className={`text-right ${retCls(g.alloc)}`}>{pct(g.alloc)}</td>
                          <td className={`text-right ${retCls(g.select)}`}>{pct(g.select)}</td>
                          <td className={`text-right ${retCls(g.interact)}`}>{pct(g.interact)}</td>
                          <td className={`text-right font-medium ${retCls(g.total)}`}>{pct(g.total)}</td>
                        </tr>
                      ))}
                      <tr className="font-semibold">
                        <td>合计</td>
                        <td colSpan={3} />
                        <td className={`text-right ${retCls(activeBrinson?.excess_total ?? null)}`}>
                          {pct(activeBrinson?.excess_total ?? null)}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </Panel>
              </div>
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                <Panel title="择时残差" meta="个股解释不掉的日收益（费用/滑点/择时）">
                  {residualOption ? <Chart option={residualOption} height={220} />
                    : <div className="text-sm text-ink-faint">无残差数据</div>}
                </Panel>
                <Panel title="成本拖累（累计）"
                  meta={att.cost ? `合计 ¥${num(att.cost.total_fee, 0)} · 拖累 ${pct(att.cost.total_drag)}` : undefined}>
                  {costOption ? <Chart option={costOption} height={220} />
                    : <div className="text-sm text-ink-faint">无费用记录（零费率运行）</div>}
                </Panel>
              </div>
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                <Panel title="行业暴露时序" meta={att.profile?.note}>
                  {industryOption ? <Chart option={industryOption} height={280} />
                    : <div className="text-sm text-ink-faint">无行业映射数据</div>}
                </Panel>
                <Panel title="持仓集中度" meta="Top5 / Top10 权重与 HHI">
                  {concOption ? <Chart option={concOption} height={280} />
                    : <div className="text-sm text-ink-faint">无持仓数据</div>}
                </Panel>
              </div>
              <Panel title="风格暴露漂移" meta="时序 z 分数标准化（量纲不同，只看漂移方向）">
                {styleExposureOption ? <Chart option={styleExposureOption} height={260} />
                  : <div className="text-sm text-ink-faint">无风格数据（日线缺估值/市值列）</div>}
              </Panel>
              <Panel title="风格收益归因" meta={att.style_attr?.note}>
                {styleAttrOption ? (
                  <>
                    <Chart option={styleAttrOption} height={300} />
                    <table className="table-dense mt-3 text-xs">
                      <thead>
                        <tr>
                          <th className="text-left">来源</th><th className="text-right">累计贡献</th>
                        </tr>
                      </thead>
                      <tbody>
                        {att.style_attr.factors.map((k) => (
                          <tr key={k} className="hover:bg-white">
                            <td>{k}</td>
                            <td className={`text-right ${retCls(att.style_attr.totals[k])}`}>
                              {pct(att.style_attr.totals[k])}
                            </td>
                          </tr>
                        ))}
                        <tr className="hover:bg-white">
                          <td>共同因子合计</td>
                          <td className={`text-right ${retCls(att.style_attr.totals.common)}`}>
                            {pct(att.style_attr.totals.common)}
                          </td>
                        </tr>
                        <tr className="font-semibold">
                          <td>特异收益（Alpha）</td>
                          <td className={`text-right ${retCls(att.style_attr.totals.specific)}`}>
                            {pct(att.style_attr.totals.specific)}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </>
                ) : (
                  <div className="text-sm text-ink-faint">
                    {att.style_attr?.note ?? '风格归因不可用'}
                  </div>
                )}
              </Panel>
              <Panel title="回撤期归因" meta={att.drawdown?.note}>
                {att.drawdown?.periods?.length ? (
                  <div className="space-y-3">
                    {att.drawdown.periods.slice(0, 5).map((p) => (
                      <div key={p.start} className="rounded border border-line p-3">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm font-medium">
                          <span>{p.start} → {p.end ?? '未收复'}</span>
                          <span className="text-down">回撤 {pct(p.drawdown, 1)}</span>
                          <span className="text-ink-dim">{p.days} 个交易日</span>
                          <span className={retCls(p.ret)}>区间收益 {pct(p.ret)}</span>
                        </div>
                        {p.factors && (
                          <div className="mt-1.5 text-xs text-ink-dim">
                            风格因子合计 <span className={retCls(p.common ?? null)}>{pct(p.common ?? null)}</span>
                            {' · '}特异 <span className={retCls(p.specific ?? null)}>{pct(p.specific ?? null)}</span>
                            {' · '}{Object.entries(p.factors).sort((a, b) => a[1] - b[1]).map(([k, v]) => `${k} ${pct(v)}`).join(' / ')}
                          </div>
                        )}
                        {(p.stock_top?.length || p.stock_bottom?.length) ? (
                          <div className="mt-2 grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
                            <div>
                              <div className="mb-1 text-ink-faint">拖累前 3</div>
                              {(p.stock_bottom ?? []).slice(0, 3).map((s) => (
                                <div key={s.symbol} className="flex justify-between">
                                  <span>{s.symbol}</span>
                                  <span className={retCls(s.contribution)}>{pct(s.contribution)}</span>
                                </div>
                              ))}
                            </div>
                            <div>
                              <div className="mb-1 text-ink-faint">抗跌前 3</div>
                              {(p.stock_top ?? []).slice(0, 3).map((s) => (
                                <div key={s.symbol} className="flex justify-between">
                                  <span>{s.symbol}</span>
                                  <span className={retCls(s.contribution)}>{pct(s.contribution)}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-sm text-ink-faint">{att.drawdown?.note ?? '无回撤期数据'}</div>
                )}
              </Panel>
              <Panel title="风险归因（方差分解，年化）" meta={att.risk_attr?.note}>
                {att.risk_attr?.factors?.length ? (
                  <>
                    <div className="mb-2 text-sm">
                      总波动 <b>{pct(att.risk_attr.vol_total)}</b>
                      {' = '}系统性 <span className="text-indigo">{pct(att.risk_attr.vol_common)}</span>
                      {' + '}特异 <span className="text-gold">{pct(att.risk_attr.vol_specific)}</span>
                      {'（cross '}{pct(att.risk_attr.cross_term, 3)}）
                      <span className="ml-2 text-xs text-ink-faint">{att.risk_attr.n_days} 个有效分解日</span>
                    </div>
                    <table className="table-dense text-xs">
                      <thead>
                        <tr>
                          <th className="text-left">因子</th><th className="text-right">平均暴露</th>
                          <th className="text-right">方差贡献（%²，年化）</th><th className="text-right">占系统性</th>
                        </tr>
                      </thead>
                      <tbody>
                        {att.risk_attr.factors.map((f) => (
                          <tr key={f.factor} className="hover:bg-white">
                            <td>{f.factor}</td>
                            <td className="text-right">{num(f.avg_exposure, 3)}</td>
                            <td className="text-right">{num(f.var_contrib, 4)}</td>
                            <td className="text-right">{f.pct == null ? '--' : `${(f.pct * 100).toFixed(1)}%`}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                ) : (
                  <div className="text-sm text-ink-faint">{att.risk_attr?.note ?? '风险归因不可用'}</div>
                )}
              </Panel>
            </>
          )}
        </>
      )}

      {tab === 'holdings' && (
        <Panel
          title="每日持仓 & 收益"
          actions={holdIdx && (
            <select className="input text-xs" value={holdDay}
              onChange={(e) => setHoldDay(e.target.value)}>
              <option value="">选择日期…</option>
              {[...holdIdx.dates].reverse().map((x) => (
                <option key={x.date} value={x.date}>{x.date} {x.day_return != null ? `(${(x.day_return * 100).toFixed(2)}%)` : ''}</option>
              ))}
            </select>
          )}
          bodyClass="p-4"
        >
          {!holdDay && holdIdx && (
            <div className="max-h-[520px] overflow-auto">
              <table className="table-dense text-xs">
                <thead>
                  <tr className="sticky top-0 bg-panel">
                    <th className="text-left">日期</th><th className="text-right">净值</th><th className="text-right">当日收益</th>
                  </tr>
                </thead>
                <tbody>
                  {holdIdx.dates.map((x) => (
                    <tr key={x.date} className="cursor-pointer hover:bg-white"
                      onClick={() => setHoldDay(x.date)}>
                      <td>{x.date}</td>
                      <td className="text-right">{num(x.nav, 0)}</td>
                      <td className={`text-right ${retCls(x.day_return)}`}>{pct(x.day_return)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {holdDay && holdDayError && (
            <div className="py-8 text-center text-sm text-ink-faint">
              加载失败：{String(holdDayError)} <button className="btn btn-sm ml-2" onClick={() => setHoldDay('')}>返回列表</button>
            </div>
          )}
          {holdDay && holdDayLoading && <Loading>持仓明细加载中…</Loading>}
          {holdDay && holdDayData && (
            <div>
              <div className="mb-3 text-xs text-ink-dim">
                {holdDayData.date} · 总资产 ¥{num(holdDayData.total_value, 0)} · 当日收益{' '}
                <span className={retCls(holdDayData.day_return)}>{pct(holdDayData.day_return)}</span>
                <button className="btn btn-sm ml-3" onClick={() => setHoldDay('')}>返回列表</button>
              </div>
              <table className="table-dense text-xs">
                <thead>
                  <tr>
                    <th className="text-left">代码</th><th className="text-right">持仓量</th>
                    <th className="text-right">收盘价</th><th className="text-right">市值</th><th className="text-right">权重</th>
                  </tr>
                </thead>
                <tbody>
                  {holdDayData.positions.map((p) => (
                    <tr key={p.symbol} className="hover:bg-white">
                      <td className="font-mono">{p.symbol}</td>
                      <td className="text-right">{num(p.qty, 0)}</td>
                      <td className="text-right">{num(p.close)}</td>
                      <td className="text-right">¥{num(p.value, 0)}</td>
                      <td className="text-right">{pct(p.weight)}</td>
                    </tr>
                  ))}
                  <tr className="font-medium">
                    <td>现金</td><td colSpan={2} />
                    <td className="text-right">
                      ¥{num(holdDayData.total_value - holdDayData.positions.reduce((s, p) => s + p.value, 0), 0)}
                    </td>
                    <td className="text-right">
                      {pct(1 - holdDayData.positions.reduce((s, p) => s + p.weight, 0))}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}

      {tab === 'trades' && (
        <Panel title="单笔进出" meta={`${d.orders.length} 笔`}>
          <div className="max-h-[560px] overflow-auto">
            <table className="table-dense text-xs">
              <thead>
                <tr className="sticky top-0 bg-panel">
                  <th className="text-left">日期</th><th className="text-left">代码</th>
                  <th className="text-right">方向</th><th className="text-right">数量</th>
                  <th className="text-right">价格</th><th className="text-right">费用</th><th className="text-right">金额</th>
                </tr>
              </thead>
              <tbody>
                {d.orders.map((o, i) => (
                  <tr key={i} className="hover:bg-white">
                    <td>{o.ts.slice(0, 10)}</td>
                    <td className="font-mono">{o.symbol}</td>
                    <td className={`text-right ${o.side === 'buy' ? 'text-up' : 'text-down'}`}>
                      {o.side === 'buy' ? '买入' : '卖出'}
                    </td>
                    <td className="text-right">{num(o.qty, 0)}</td>
                    <td className="text-right">{num(o.price, 3)}</td>
                    <td className="text-right">{num(o.fee, 2)}</td>
                    <td className="text-right">¥{num(o.qty * o.price, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {tab === 'perf' && (
        <>
          <Panel title="滚动风险" meta="20 日窗口">
            {rollingOption ? <Chart option={rollingOption} height={280} />
              : <div className="text-sm text-ink-faint">净值不足 20 日</div>}
          </Panel>
          <Panel title="回撤水下图">
            {ddOption && <Chart option={ddOption} height={220} />}
          </Panel>
          <Panel title="更多指标">
            <div className="grid grid-cols-2 divide-line sm:grid-cols-4 sm:divide-x">
              {[['卡玛比率', num(m.calmar)], ['波动率(年化)', pct(m.annual_vol)],
                ['偏度', num(m.skew)], ['峰度', num(m.kurtosis)],
                ['最佳单日', pct(m.best_period)], ['最差单日', pct(m.worst_period)],
                ['最长水下(日)', num(m.longest_dd_periods, 0)], ['交易笔数', num(m.n_trades, 0)]].map(([k, v]) => (
                <div key={k} className="px-3 py-2">
                  <div className="text-xs text-ink-faint">{k}</div>
                  <div className="mt-0.5 font-semibold tabular-nums">{v}</div>
                </div>
              ))}
            </div>
          </Panel>
        </>
      )}

      {tab === 'code' && (
        <Panel title="策略代码"
          meta={`${codeInfo?.benchmark ? `基准 ${codeInfo.benchmark}` : ''}${codeInfo?.engine === 'jq_compat' ? ' · 聚宽兼容引擎' : ''}`}>
          <pre className="max-h-[600px] overflow-auto rounded-[2px] bg-ink p-4 text-xs leading-5 text-paper">
            {codeInfo?.code ?? '（非代码策略）'}
          </pre>
        </Panel>
      )}
    </div>
  );
}
