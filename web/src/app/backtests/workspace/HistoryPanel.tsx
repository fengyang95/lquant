// 回测记录表 + 多运行对比 —— 逻辑原样迁自旧回测页对应两个 Panel；自持 useSWR('/backtests')。
// 新增：onLoadRun prop + 行尾「载入」按钮，供工作台把历史运行载入编辑器。
'use client';

import { useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote } from '@/components/States';
import { get } from '@/lib/api';
import { SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';

export type RunRow = {
  run_id: string;
  strategy: string;
  params: { factor?: string; top_n?: number; rebalance?: string; strategy_id?: string };
  start_date: string;
  end_date: string;
  status: string;
  metrics: Record<string, unknown>;
  created_at: string;
};

type CompareResult = {
  runs: { run_id: string; label: string; metrics: Record<string, number> }[];
  dates: string[];
  series: Record<string, (number | null)[]>;
};

type HistoryPanelProps = {
  onLoadRun(row: RunRow): void;
};

// 指标单元格统一出口：null/undefined/非有限值一律 `--`，
// 运行中任务的空 metrics 不再伪装成 0.00%。
function pctf(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${(v * 100).toFixed(digits)}%`;
}

function numf(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return v.toFixed(digits);
}

// metrics.turnover 落库的是 {total_amount, turnover_per_period, unit} 结构 ——
// 展示时取无量纲比率；老数据/异常结构统一走 pctf 的 `--` 兜底。
function turnoverOf(m: Record<string, unknown>): number | null | undefined {
  const t = m?.turnover;
  if (t && typeof t === 'object') return (t as { turnover_per_period?: number | null }).turnover_per_period;
  return t as number | null | undefined;
}

function isRunning(status?: string): boolean {
  return status === 'running' || status === 'pending' || status === 'started' || status === 'queued';
}

export default function HistoryPanel({ onLoadRun }: HistoryPanelProps) {
  // 仅当存在运行中/排队中的任务时才轮询，全部终态即停 —— 不给后端白打请求
  const { data: runs } = useSWR<RunRow[]>('/backtests', get, {
    refreshInterval: (latest) =>
      (latest && Array.isArray(latest) && latest.some((r) => isRunning(r.status))) ? 5000 : 0,
  });
  // B6 多运行对比
  const [picked, setPicked] = useState<string[]>([]);
  const [cmp, setCmp] = useState<CompareResult | null>(null);
  const [cmpBusy, setCmpBusy] = useState(false);
  const [err, setErr] = useState('');

  function toggle(id: string) {
    setCmp(null);
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length >= 6 ? p : [...p, id]));
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
    tooltip,
    legend: legend({ top: 0, data: cmp.runs.map((r) => r.label) }),
    grid: { left: 60, right: 20, top: 36, bottom: 30 },
    ...axes({ data: cmp.dates }, { scale: true, name: '净值(归一)' }),
    series: cmp.runs.map((r, i) => ({
      name: r.label,
      type: 'line' as const,
      data: cmp.series[r.run_id] ?? [],
      showSymbol: false,
      lineStyle: { width: 1.5, color: SERIES_COLORS[i % SERIES_COLORS.length] },
      itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    })),
  } : null;

  return (
    <>
      {err && <ErrorNote>{err}</ErrorNote>}

      <Panel
        title="回测记录"
        meta={`共 ${runs?.length ?? 0} 条${picked.length > 0 ? ` · 已选 ${picked.length}/6` : ''}`}
        actions={picked.length >= 2 ? (
          <button onClick={compare} disabled={cmpBusy} className="btn btn-primary btn-sm">
            {cmpBusy ? '生成对比…' : `对比选中 ${picked.length} 项`}
          </button>
        ) : null}
        bodyClass="p-0"
      >
        {!runs?.length ? (
          <div className="p-4"><Empty>还没有回测 —— 用上方表单跑一个</Empty></div>
        ) : (
          <div className="overflow-x-auto p-4">
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="w-10 pl-1 text-left">对比</th>
                  <th className="text-left">Run</th>
                  <th className="text-left">因子 / 参数</th>
                  <th className="text-right">总收益</th>
                  <th className="text-right">年化</th>
                  <th className="text-right">夏普</th>
                  <th className="text-right">最大回撤</th>
                  <th className="text-right">时间</th>
                  <th className="w-14 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id} className="hover:bg-white">
                    <td className="pl-1">
                      <input type="checkbox" checked={picked.includes(r.run_id)}
                             onChange={() => toggle(r.run_id)}
                             className="h-3.5 w-3.5 accent-up" />
                    </td>
                    <td>
                      <Link href={`/backtests/${r.run_id}`} className="font-mono text-xs text-indigo hover:underline">
                        {r.run_id}
                      </Link>
                    </td>
                    <td className="text-xs text-ink-dim">
                      {r.params?.factor || r.strategy || '--'} · Top{r.params?.top_n ?? '--'} · {r.params?.rebalance ?? '--'}
                    </td>
                    {isRunning(r.status) ? (
                      <td colSpan={4} className="text-right text-xs text-ink-faint">运行中…</td>
                    ) : (
                      <>
                        <td className={`text-right ${typeof r.metrics.total_return === 'number' && r.metrics.total_return >= 0 ? 'text-up' : 'text-down'}`}>
                          {pctf(r.metrics.total_return as number | null)}
                        </td>
                        <td className={`text-right ${typeof r.metrics.annual_return === 'number' && r.metrics.annual_return >= 0 ? 'text-up' : 'text-down'}`}>
                          {pctf(r.metrics.annual_return as number | null)}
                        </td>
                        <td className="text-right">{numf(r.metrics.sharpe as number | null)}</td>
                        <td className="text-right text-down">{pctf(r.metrics.max_drawdown as number | null)}</td>
                      </>
                    )}
                    <td className="text-right text-xs text-ink-faint">{r.created_at?.slice(5, 16)}</td>
                    <td className="text-right pr-1">
                      <button onClick={() => onLoadRun(r)} className="btn btn-sm">
                        载入
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* B6 对比结果 */}
      {cmp && (
        <Panel title="运行对比" meta="净值按各自首日归一">
          <Chart option={cmpOption} height={320} />
          <table className="table-dense mt-4">
            <thead>
              <tr>
                <th className="text-left">Run</th>
                <th className="text-right">年化</th>
                <th className="text-right">夏普</th>
                <th className="text-right">最大回撤</th>
                <th className="text-right">胜率</th>
                <th className="text-right">换手</th>
              </tr>
            </thead>
            <tbody>
              {cmp.runs.map((r) => (
                <tr key={r.run_id} className="hover:bg-white">
                  <td>
                    <span className="mr-1.5 inline-block h-2 w-2 rounded-[1px]"
                          style={{ background: SERIES_COLORS[cmp.runs.indexOf(r) % SERIES_COLORS.length] }} />
                    {r.label}
                  </td>
                  <td className={`text-right ${r.metrics.annual_return != null && r.metrics.annual_return >= 0 ? 'text-up' : 'text-down'}`}>
                    {pctf(r.metrics.annual_return)}
                  </td>
                  <td className="text-right">{numf(r.metrics.sharpe)}</td>
                  <td className="text-right text-down">{pctf(r.metrics.max_drawdown)}</td>
                  <td className="text-right">{pctf(r.metrics.win_rate, 0)}</td>
                  <td className="text-right">{pctf(turnoverOf(r.metrics), 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </>
  );
}
