'use client';

/**
 * 回测工作台右侧运行结果区 —— ResultPane：
 * runId 为空 → 空态引导；非空 → SWR 拉详情，渲染六项指标卡 + 净值图 + 日志折叠 + 详情链接。
 * 颜色沿用 A 股习惯：红涨绿跌（text-up / text-down）。
 */

import { useMemo } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { get } from '@/lib/api';
import { C, legend, tooltip } from '@/lib/chart';

type NavPt = { date: string; nav: number; drawdown: number | null };

type Detail = {
  run_id: string;
  strategy: string;
  params: Record<string, unknown>;
  status: string;
  metrics: Record<string, number>;
  nav: NavPt[];
  benchmark: { date: string; nav: number }[];
  benchmark_label: string;
  logs?: string[];
};

/** 涨红跌绿的文字色类（无值时中性） */
const retCls = (v: number | null | undefined) =>
  v == null ? '' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : '';

const pct = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : `${(v * 100).toFixed(digits)}%`;

const num = (v: number | null | undefined, digits = 2) =>
  v == null || !isFinite(v) ? '--' : v.toFixed(digits);

/** 净值图：策略/基准/超额三线 + 超额右轴（百分比），与详情页同形但独立实现 */
function useNavOption(d: Detail | undefined) {
  return useMemo(() => {
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
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLine: { lineStyle: { color: C.line } },
        axisTick: { show: false },
        axisLabel: { color: C.inkDim, fontSize: 10 },
      },
      yAxis: [
        {
          type: 'value' as const,
          scale: true,
          axisLine: { show: false },
          splitLine: { lineStyle: { color: C.line } },
          axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
        },
        {
          type: 'value' as const,
          scale: true,
          axisLine: { show: false },
          splitLine: { show: false },
          axisLabel: {
            color: C.inkDim,
            fontSize: 10,
            formatter: (v: number) => `${(v * 100).toFixed(0)}%`,
          },
        },
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: strat,
          showSymbol: false,
          lineStyle: { width: 1.6, color: C.indigo },
          itemStyle: { color: C.indigo },
        },
        {
          name: d.benchmark_label,
          type: 'line',
          data: benchY,
          showSymbol: false,
          lineStyle: { width: 1.2, color: C.inkFaint },
          itemStyle: { color: C.inkFaint },
        },
        {
          name: '超额收益',
          type: 'line',
          yAxisIndex: 1,
          data: excess,
          showSymbol: false,
          lineStyle: { width: 1.2, color: C.gold },
          areaStyle: { color: 'rgba(176,138,62,0.08)' },
          itemStyle: { color: C.gold },
        },
      ],
    };
  }, [d]);
}

export default function ResultPane({ runId }: { runId: string | null }) {
  const { data: d, isLoading, error } = useSWR<Detail>(
    runId ? `/backtests/${runId}` : null,
    get,
    // 详情未就绪时轮询刷新（HistoryPanel 同款），拿到结果自动停
    { refreshInterval: (latest?: Detail) =>
        latest?.status && latest.status !== 'done' && latest.status !== 'failed' ? 3000 : 0 },
  );
  const navOption = useNavOption(d);

  if (!runId) {
    return <Empty>点「编译运行 ▶」开始第一次回测</Empty>;
  }

  if (error) {
    return <ErrorNote>加载失败：{error instanceof Error ? error.message : String(error)}</ErrorNote>;
  }

  if (isLoading || !d) {
    return <Loading>加载中…</Loading>;
  }

  const m = d.metrics ?? {};
  const logs = d.logs ?? [];

  const METRICS: { label: string; value: string; tone?: string; hint?: string }[] = [
    { label: '收益', value: pct(m.total_return), tone: retCls(m.total_return) },
    { label: '年化', value: pct(m.annual_return), tone: retCls(m.annual_return) },
    { label: '夏普', value: num(m.sharpe) },
    { label: '回撤', value: pct(m.max_drawdown), tone: 'text-down' },
    { label: '胜率', value: pct(m.win_rate) },
    { label: '费用', value: `¥${num(m.total_fee, 0)}`, hint: m.n_trades != null ? `成交 ${m.n_trades} 笔` : undefined },
  ];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-x-4 gap-y-3 border border-line bg-panel px-4 py-3">
        {METRICS.map((s) => (
          <Stat key={s.label} label={s.label} value={s.value} tone={s.tone} hint={s.hint} />
        ))}
      </div>

      {navOption && (
        <section className="border border-line bg-panel p-3">
          <Chart option={navOption} height={240} />
        </section>
      )}

      {logs.length > 0 && (
        <details className="border border-line bg-panel px-4 py-2 text-sm">
          <summary className="cursor-pointer text-ink-dim select-none">
            运行日志（{logs.length} 条）
          </summary>
          <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap text-xs text-ink-faint">
            {logs.join('\n')}
          </pre>
        </details>
      )}

      <Link
        href={`/backtests/${runId}`}
        className="block border border-line bg-panel px-4 py-2 text-center text-sm text-ink hover:bg-panel-strong"
      >
        查看完整详情 →
      </Link>
    </div>
  );
}
