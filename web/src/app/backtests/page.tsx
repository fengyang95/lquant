'use client';

import { useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import ReactECharts from 'echarts-for-react';
import { get, post } from '@/lib/api';

type RunRow = {
  run_id: string;
  strategy: string;
  params: { factor?: string; top_n?: number; rebalance?: string };
  start_date: string;
  end_date: string;
  status: string;
  metrics: Record<string, number>;
  created_at: string;
};

type CompareResult = {
  runs: { run_id: string; label: string; metrics: Record<string, number> }[];
  dates: string[];
  series: Record<string, (number | null)[]>;
};

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20'];
const REBALANCES = ['daily', 'weekly', 'monthly'];
const LINE_COLORS = ['#dc2626', '#2563eb', '#16a34a', '#9333ea', '#ea580c', '#0d9488'];

export default function BacktestsPage() {
  const { data: runs, mutate } = useSWR<RunRow[]>('/backtests', get, { refreshInterval: 5000 });
  const [formula, setFormula] = useState('pct_change_20');
  const [topN, setTopN] = useState(5);
  const [rebalance, setRebalance] = useState('monthly');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  // B6 多运行对比
  const [picked, setPicked] = useState<string[]>([]);
  const [cmp, setCmp] = useState<CompareResult | null>(null);
  const [cmpBusy, setCmpBusy] = useState(false);

  function toggle(id: string) {
    setCmp(null);
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length >= 6 ? p : [...p, id]));
  }

  async function run() {
    setBusy(true);
    setErr('');
    try {
      await post('/backtests/run', { top_n: topN, rebalance, formula });
      mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function compare() {
    setCmpBusy(true);
    setErr('');
    try {
      setCmp(await get<CompareResult>(`/backtests/compare?ids=${picked.join(',')}`));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCmpBusy(false);
    }
  }

  const cmpOption = cmp ? {
    grid: { left: 60, right: 20, top: 36, bottom: 30 },
    legend: { top: 0, data: cmp.runs.map((r) => r.label) },
    tooltip: { trigger: 'axis' as const },
    xAxis: { type: 'category' as const, data: cmp.dates },
    yAxis: { type: 'value' as const, scale: true, name: '净值(归一)' },
    series: cmp.runs.map((r, i) => ({
      name: r.label,
      type: 'line' as const,
      data: cmp.series[r.run_id],
      showSymbol: false,
      lineStyle: { width: 1.5, color: LINE_COLORS[i % LINE_COLORS.length] },
      itemStyle: { color: LINE_COLORS[i % LINE_COLORS.length] },
    })),
  } : null;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">回测</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border bg-white p-4">
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">因子公式</div>
          <select value={formula} onChange={(e) => setFormula(e.target.value)}
                  className="rounded-md border px-3 py-2 text-sm">
            {FORMULAS.map((f) => <option key={f}>{f}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">TopN</div>
          <input type="number" min={1} max={50} value={topN}
                 onChange={(e) => setTopN(+e.target.value)}
                 className="w-20 rounded-md border px-3 py-2 text-sm" />
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">调仓频率</div>
          <select value={rebalance} onChange={(e) => setRebalance(e.target.value)}
                  className="rounded-md border px-3 py-2 text-sm">
            {REBALANCES.map((r) => <option key={r}>{r}</option>)}
          </select>
        </label>
        <button onClick={run} disabled={busy}
                className="rounded-md bg-red-600 px-4 py-2 text-sm text-white hover:bg-red-500 disabled:opacity-40">
          {busy ? '回测中…' : '运行回测'}
        </button>
        <span className="text-xs text-neutral-400">T+1 开盘价撮合 · 真实费率 · 涨跌停拒单</span>
        {err && <span className="text-sm text-red-500">{err}</span>}
      </div>

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">
            回测记录（{runs?.length ?? 0}）{picked.length > 0 && <span className="ml-2 text-xs text-neutral-400">已选 {picked.length}/6</span>}
          </span>
          {picked.length >= 2 && (
            <button onClick={compare} disabled={cmpBusy}
                    className="rounded-md bg-neutral-900 px-3 py-1.5 text-xs text-white hover:bg-neutral-700 disabled:opacity-40">
              {cmpBusy ? '生成对比…' : `对比选中 ${picked.length} 项`}
            </button>
          )}
        </div>
        {!runs?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">还没有回测 —— 用上方表单跑一个</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 pl-1 text-left font-normal">对比</th>
                <th className="text-left font-normal">Run</th>
                <th className="text-left font-normal">因子 / 参数</th>
                <th className="text-right font-normal">总收益</th>
                <th className="text-right font-normal">年化</th>
                <th className="text-right font-normal">夏普</th>
                <th className="text-right font-normal">最大回撤</th>
                <th className="text-right font-normal">时间</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id} className="border-b border-neutral-50 hover:bg-neutral-50">
                  <td className="pl-1">
                    <input type="checkbox" checked={picked.includes(r.run_id)}
                           onChange={() => toggle(r.run_id)}
                           className="h-3.5 w-3.5 accent-red-600" />
                  </td>
                  <td className="py-1.5">
                    <Link href={`/backtests/${r.run_id}`} className="font-mono text-xs text-blue-600 hover:underline">
                      {r.run_id}
                    </Link>
                  </td>
                  <td className="text-xs text-neutral-500">
                    {r.params.factor} · Top{r.params.top_n} · {r.params.rebalance}
                  </td>
                  <td className={`text-right tabular-nums ${(r.metrics.total_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                    {((r.metrics.total_return ?? 0) * 100).toFixed(2)}%
                  </td>
                  <td className={`text-right tabular-nums ${(r.metrics.annual_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                    {((r.metrics.annual_return ?? 0) * 100).toFixed(2)}%
                  </td>
                  <td className="text-right tabular-nums">{(r.metrics.sharpe ?? 0).toFixed(2)}</td>
                  <td className="text-right tabular-nums text-down">
                    {((r.metrics.max_drawdown ?? 0) * 100).toFixed(2)}%
                  </td>
                  <td className="text-xs text-neutral-400">{r.created_at?.slice(5, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* B6 对比结果 */}
      {cmp && (
        <div className="space-y-3 rounded-xl border bg-white p-4">
          <div className="text-sm font-medium">运行对比（净值按各自首日归一）</div>
          <ReactECharts option={cmpOption} style={{ height: 320 }} notMerge />
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">Run</th>
                <th className="text-right font-normal">年化</th>
                <th className="text-right font-normal">夏普</th>
                <th className="text-right font-normal">最大回撤</th>
                <th className="text-right font-normal">胜率</th>
                <th className="text-right font-normal">换手</th>
              </tr>
            </thead>
            <tbody>
              {cmp.runs.map((r) => (
                <tr key={r.run_id} className="border-b border-neutral-50">
                  <td className="py-1.5">
                    <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: LINE_COLORS[cmp.runs.indexOf(r) % LINE_COLORS.length] }} />
                    {r.label}
                  </td>
                  <td className={`text-right tabular-nums ${(r.metrics.annual_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                    {((r.metrics.annual_return ?? 0) * 100).toFixed(2)}%
                  </td>
                  <td className="text-right tabular-nums">{(r.metrics.sharpe ?? 0).toFixed(2)}</td>
                  <td className="text-right tabular-nums text-down">{((r.metrics.max_drawdown ?? 0) * 100).toFixed(2)}%</td>
                  <td className="text-right tabular-nums">{((r.metrics.win_rate ?? 0) * 100).toFixed(0)}%</td>
                  <td className="text-right tabular-nums">{((r.metrics.turnover ?? 0) * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
