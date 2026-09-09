'use client';

/**
 * 回测列表 —— 「研报台」版式：运行表单 + 记录表 + 多运行对比（B6）。
 * 数据逻辑（SWR / state / 接口）与旧版一致，仅重构呈现层。
 */

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, ErrorNote } from '@/components/States';
import { get, post } from '@/lib/api';
import { SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';

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

type StrategyMeta = {
  id?: string;
  name: string;
  source: 'builtin' | 'user';
  description?: string;
};

type RunCodeResult = { run_id: string };

type CompareResult = {
  runs: { run_id: string; label: string; metrics: Record<string, number> }[];
  dates: string[];
  series: Record<string, (number | null)[]>;
};

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20'];
const REBALANCES = ['daily', 'weekly', 'monthly'];

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
  // 策略库运行
  const { data: strategies } = useSWR<StrategyMeta[]>('/strategies', get);
  const userStrategies = (strategies ?? []).filter((s) => s.source === 'user');
  const [strategyId, setStrategyId] = useState('');
  const [runStrategyBusy, setRunStrategyBusy] = useState(false);
  const router = useRouter();

  async function runStrategy() {
    if (!strategyId) return;
    setRunStrategyBusy(true);
    setErr('');
    try {
      const detail = await get<{
        source: string;
        benchmark?: string;
        config?: { factor_formulas?: unknown };
      }>(`/strategies/${strategyId}`);
      const rawFormulas = detail.config?.factor_formulas;
      const factorFormulas = Array.isArray(rawFormulas)
        ? rawFormulas.filter((f): f is string => typeof f === 'string')
        : [];
      const body: Record<string, unknown> = {
        code: detail.source,
        start: '2024-01-01',
        end: '2024-12-31',
        strategy_id: strategyId,
      };
      if (factorFormulas.length > 0) body.factor_formulas = factorFormulas;
      const r = await post<RunCodeResult>('/backtests/run-code', body);
      router.push(`/backtests/${r.run_id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRunStrategyBusy(false);
    }
  }

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
    tooltip,
    legend: legend({ top: 0, data: cmp.runs.map((r) => r.label) }),
    grid: { left: 60, right: 20, top: 36, bottom: 30 },
    ...axes({ data: cmp.dates }, { scale: true, name: '净值(归一)' }),
    series: cmp.runs.map((r, i) => ({
      name: r.label,
      type: 'line' as const,
      data: cmp.series[r.run_id],
      showSymbol: false,
      lineStyle: { width: 1.5, color: SERIES_COLORS[i % SERIES_COLORS.length] },
      itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    })),
  } : null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="回测"
        sub="T+1 开盘价撮合 · 真实费率 · 涨跌停拒单"
      />

      {err && <ErrorNote>{err}</ErrorNote>}

      {/* 运行回测 */}
      <Panel title="运行回测">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">因子公式</div>
            <select value={formula} onChange={(e) => setFormula(e.target.value)} className="input">
              {FORMULAS.map((f) => <option key={f}>{f}</option>)}
            </select>
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">TopN</div>
            <input type="number" min={1} max={50} value={topN}
                   onChange={(e) => setTopN(+e.target.value)}
                   className="input input-mono w-20" />
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">调仓频率</div>
            <select value={rebalance} onChange={(e) => setRebalance(e.target.value)} className="input">
              {REBALANCES.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
          <button onClick={run} disabled={busy} className="btn btn-accent">
            {busy ? '回测中…' : '运行回测'}
          </button>
        </div>
      </Panel>

      {/* 策略库运行入口 */}
      <Panel title="从策略库运行" meta="自定义 Python 策略">
        {userStrategies.length === 0 ? (
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs text-ink-faint">策略库还是空的 —— 先到编辑器保存一个策略</span>
            <Link href="/strategies/editor" className="btn btn-sm btn-primary">
              打开策略编辑器
            </Link>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-sm">
              <div className="mb-1 text-xs text-ink-faint">用户策略</div>
              <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)} className="input w-56">
                <option value="">选择策略…</option>
                {userStrategies.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </label>
            <button
              onClick={runStrategy}
              disabled={!strategyId || runStrategyBusy}
              className="btn btn-accent"
            >
              {runStrategyBusy ? '运行中…' : '运行策略'}
            </button>
            <Link href="/strategies/editor" className="btn btn-sm">
              编辑器
            </Link>
          </div>
        )}
      </Panel>

      {/* 回测记录 */}
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
                      {r.params.factor} · Top{r.params.top_n} · {r.params.rebalance}
                    </td>
                    <td className={`text-right ${(r.metrics.total_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                      {((r.metrics.total_return ?? 0) * 100).toFixed(2)}%
                    </td>
                    <td className={`text-right ${(r.metrics.annual_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                      {((r.metrics.annual_return ?? 0) * 100).toFixed(2)}%
                    </td>
                    <td className="text-right">{(r.metrics.sharpe ?? 0).toFixed(2)}</td>
                    <td className="text-right text-down">
                      {((r.metrics.max_drawdown ?? 0) * 100).toFixed(2)}%
                    </td>
                    <td className="text-right text-xs text-ink-faint">{r.created_at?.slice(5, 16)}</td>
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
                  <td className={`text-right ${(r.metrics.annual_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                    {((r.metrics.annual_return ?? 0) * 100).toFixed(2)}%
                  </td>
                  <td className="text-right">{(r.metrics.sharpe ?? 0).toFixed(2)}</td>
                  <td className="text-right text-down">{((r.metrics.max_drawdown ?? 0) * 100).toFixed(2)}%</td>
                  <td className="text-right">{((r.metrics.win_rate ?? 0) * 100).toFixed(0)}%</td>
                  <td className="text-right">{((r.metrics.turnover ?? 0) * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}
