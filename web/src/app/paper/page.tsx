'use client';

import { useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { post } from '@/lib/api';

type ReplayResp = {
  summary: {
    final_nav: number;
    total_return: number;
    n_orders: number;
    n_filled: number;
    n_rejected: number;
    max_drawdown?: number;
    rejections: { order_id: string; symbol: string; reason: string }[];
  };
  positions: { symbol: string; qty: number; available: number; avg_cost: number; last_price: number; pnl_pct: number }[];
  nav: { trade_date: string; nav: number }[];
  alerts: unknown[];
};

type CompareResp = {
  nav_deviation: { max_nav_dev: number; corr: number | null; n_mismatch_days: number; verdict: string; detail: string };
  trade_comparison: { bt_trades: number; paper_trades: number; match_rate: number };
};

export default function PaperPage() {
  const [topN, setTopN] = useState(5);
  const [runId, setRunId] = useState('');
  const [replay, setReplay] = useState<ReplayResp | null>(null);
  const [cmp, setCmp] = useState<CompareResp | null>(null);
  const [busy, setBusy] = useState<'' | 'replay' | 'cmp'>('');
  const [err, setErr] = useState('');

  async function doReplay() {
    setBusy('replay');
    setErr('');
    setCmp(null);
    try {
      setReplay(await post<ReplayResp>('/paper/replay', { top_n: topN }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }

  async function doCompare() {
    setBusy('cmp');
    setErr('');
    try {
      setCmp(await post<CompareResp>('/paper/compare', { run_id: runId.trim() }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }

  const navOption = replay
    ? {
        grid: { left: 80, right: 20, top: 20, bottom: 30 },
        tooltip: { trigger: 'axis' as const },
        xAxis: { type: 'category' as const, data: replay.nav.map((p) => p.trade_date) },
        yAxis: { type: 'value' as const, scale: true },
        series: [{ name: '模拟盘净值', type: 'line', data: replay.nav.map((p) => p.nav), showSymbol: false }],
      }
    : null;

  const verdictColor = (v: string) =>
    v === 'ok' ? 'text-down' : v === 'warning' ? 'text-amber-500' : 'text-up';

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">模拟盘</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border bg-white p-4">
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">TopN</div>
          <input type="number" min={1} max={20} value={topN}
                 onChange={(e) => setTopN(+e.target.value)}
                 className="w-20 rounded-md border px-3 py-2 text-sm" />
        </label>
        <button onClick={doReplay} disabled={busy === 'replay'}
                className="rounded-md bg-red-600 px-4 py-2 text-sm text-white hover:bg-red-500 disabled:opacity-40">
          {busy === 'replay' ? '回放中…' : '离线回放'}
        </button>
        <div className="mx-2 h-8 w-px bg-neutral-200" />
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">回测 Run ID（对拍用）</div>
          <input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="先跑一次回测"
                 className="w-56 rounded-md border px-3 py-2 font-mono text-xs" />
        </label>
        <button onClick={doCompare} disabled={busy === 'cmp' || !runId.trim() || !replay}
                className="rounded-md bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700 disabled:opacity-40">
          {busy === 'cmp' ? '对拍中…' : '与回测对拍'}
        </button>
        {err && <span className="text-sm text-red-500">{err}</span>}
      </div>

      {replay && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            {[
              { label: '期末净值', value: replay.summary.final_nav.toLocaleString() },
              { label: '总收益', value: `${(replay.summary.total_return * 100).toFixed(2)}%` },
              { label: '委托 / 成交', value: `${replay.summary.n_orders} / ${replay.summary.n_filled}` },
              { label: '拒单', value: String(replay.summary.n_rejected) },
              { label: '最大回撤', value: replay.summary.max_drawdown != null ? `${(replay.summary.max_drawdown * 100).toFixed(3)}%` : '—' },
            ].map((c) => (
              <div key={c.label} className="rounded-xl border bg-white p-3">
                <div className="text-xs text-neutral-400">{c.label}</div>
                <div className="text-lg font-semibold tabular-nums">{c.value}</div>
              </div>
            ))}
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <div className="rounded-xl border bg-white p-4">
              <ReactECharts option={navOption!} style={{ height: 280 }} notMerge />
            </div>
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-medium">持仓（{replay.positions.length}）</div>
              <table className="w-full text-sm">
                <thead className="text-xs text-neutral-400">
                  <tr className="border-b">
                    <th className="py-1.5 text-left font-normal">标的</th>
                    <th className="text-right font-normal">持仓/可卖</th>
                    <th className="text-right font-normal">成本</th>
                    <th className="text-right font-normal">现价</th>
                    <th className="text-right font-normal">盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {replay.positions.map((p) => (
                    <tr key={p.symbol} className="border-b border-neutral-50">
                      <td className="py-1.5 font-mono text-xs">{p.symbol}</td>
                      <td className="text-right tabular-nums">{p.qty} / {p.available}</td>
                      <td className="text-right tabular-nums">{p.avg_cost.toFixed(2)}</td>
                      <td className="text-right tabular-nums">{p.last_price.toFixed(2)}</td>
                      <td className={`text-right tabular-nums ${p.pnl_pct >= 0 ? 'text-up' : 'text-down'}`}>
                        {(p.pnl_pct * 100).toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {cmp && (
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-medium">
                对拍结果：
                <span className={`ml-1 font-semibold ${verdictColor(cmp.nav_deviation.verdict)}`}>
                  {cmp.nav_deviation.verdict.toUpperCase()}
                </span>
              </div>
              <p className="text-sm text-neutral-600">{cmp.nav_deviation.detail}</p>
              <div className="mt-2 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
                <div><span className="text-xs text-neutral-400">最大 NAV 偏差 </span>{(cmp.nav_deviation.max_nav_dev * 100).toFixed(2)}%</div>
                <div><span className="text-xs text-neutral-400">净值相关性 </span>{cmp.nav_deviation.corr ?? '—'}</div>
                <div><span className="text-xs text-neutral-400">超阈天数 </span>{cmp.nav_deviation.n_mismatch_days}</div>
                <div><span className="text-xs text-neutral-400">成交匹配率 </span>{(cmp.trade_comparison.match_rate * 100).toFixed(0)}%</div>
              </div>
            </div>
          )}
        </>
      )}

      {!replay && (
        <div className="rounded-xl border border-dashed bg-white py-16 text-center text-sm text-neutral-400">
          模拟盘验证链路：离线回放（撮合 / T+N / 费率 / 拒单）→ 与回测对拍 → 偏差可解释才谈实盘
        </div>
      )}
    </div>
  );
}
