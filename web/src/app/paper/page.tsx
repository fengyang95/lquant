'use client';

import { useState } from 'react';
import Chart from '@/components/Chart';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Msg } from '@/components/States';
import { post } from '@/lib/api';
import { C, axes, tooltip } from '@/lib/chart';

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

/** 对拍结论配色：ok 正常 / warning 金 / 越界朱砂 */
const verdictTone = (v: string) =>
  v === 'ok' ? 'text-ink' : v === 'warning' ? 'text-gold' : 'text-up';

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
        tooltip,
        grid: { left: 70, right: 20, top: 20, bottom: 30 },
        ...axes({ data: replay.nav.map((p) => p.trade_date) }, { scale: true }),
        series: [{
          name: '模拟盘净值', type: 'line', data: replay.nav.map((p) => p.nav),
          showSymbol: false, lineStyle: { width: 1.6, color: C.indigo }, itemStyle: { color: C.indigo },
        }],
      }
    : null;

  const sum = replay?.summary;

  return (
    <div className="space-y-5">
      <PageHeader
        title="模拟盘"
        sub="离线回放（撮合 / T+N / 费率 / 拒单）→ 与回测对拍 → 偏差可解释才谈实盘"
      />

      {err ? <ErrorNote>{err}</ErrorNote> : null}

      {/* 操作条 */}
      <Panel bodyClass="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">TopN</div>
            <input type="number" min={1} max={20} value={topN}
              onChange={(e) => setTopN(+e.target.value)}
              className="input input-mono w-20" />
          </label>
          <button onClick={doReplay} disabled={busy === 'replay'} className="btn btn-accent">
            {busy === 'replay' ? '回放中…' : '离线回放'}
          </button>
          <div className="mx-2 h-8 w-px bg-line-strong" />
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">回测 Run ID（对拍用）</div>
            <input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="先跑一次回测"
              className="input input-mono w-56" />
          </label>
          <button onClick={doCompare} disabled={busy === 'cmp' || !runId.trim() || !replay} className="btn btn-primary">
            {busy === 'cmp' ? '对拍中…' : '与回测对拍'}
          </button>
        </div>
      </Panel>

      {!replay ? (
        <Empty>设置 TopN 后点「离线回放」，用历史日线完整重放一遍撮合链路</Empty>
      ) : (
        <>
          {/* 回放指标：一个面板分栏，不拆卡片 */}
          <Panel title="回放结果" bodyClass="p-4">
            <div className="grid grid-cols-2 gap-y-4 divide-line sm:grid-cols-3 lg:grid-cols-5 sm:divide-x">
              <div className="pr-4">
                <Stat label="期末净值" value={sum!.final_nav.toLocaleString()} />
              </div>
              <div className="px-4">
                <Stat label="总收益" value={`${(sum!.total_return * 100).toFixed(2)}%`}
                  tone={sum!.total_return >= 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="委托 / 成交" value={`${sum!.n_orders} / ${sum!.n_filled}`} />
              </div>
              <div className="px-4">
                <Stat label="拒单" value={String(sum!.n_rejected)} tone={sum!.n_rejected > 0 ? 'text-gold' : 'text-ink'} />
              </div>
              <div className="px-4">
                <Stat label="最大回撤"
                  value={sum!.max_drawdown != null ? `${(sum!.max_drawdown * 100).toFixed(3)}%` : '—'}
                  tone="text-down" />
              </div>
            </div>
          </Panel>

          <div className="grid gap-5 md:grid-cols-2">
            <Panel title="净值曲线">
              <Chart option={navOption} height={280} />
            </Panel>
            <Panel title="持仓" meta={`${replay.positions.length} 只`} bodyClass="">
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="pl-4 text-left">标的</th>
                    <th className="text-right">持仓/可卖</th>
                    <th className="text-right">成本</th>
                    <th className="text-right">现价</th>
                    <th className="pr-4 text-right">盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {replay.positions.map((p) => (
                    <tr key={p.symbol} className="hover:bg-white">
                      <td className="py-2 pl-4 font-mono text-xs">{p.symbol}</td>
                      <td className="text-right">{p.qty} / {p.available}</td>
                      <td className="text-right">{p.avg_cost.toFixed(2)}</td>
                      <td className="text-right">{p.last_price.toFixed(2)}</td>
                      <td className={`pr-4 text-right ${p.pnl_pct >= 0 ? 'text-up' : 'text-down'}`}>
                        {(p.pnl_pct * 100).toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          </div>

          {cmp && (
            <Panel title="与回测对拍">
              <p className="text-sm text-ink-dim">
                <span className={`font-song text-lg font-semibold ${verdictTone(cmp.nav_deviation.verdict)}`}>
                  {cmp.nav_deviation.verdict.toUpperCase()}
                </span>
                <span className="ml-3">{cmp.nav_deviation.detail}</span>
              </p>
              <div className="mt-3 grid grid-cols-2 gap-y-3 divide-line border-t border-line pt-3 text-sm sm:grid-cols-4 sm:divide-x">
                <div className="pr-4">
                  <div className="text-xs text-ink-faint">最大 NAV 偏差</div>
                  <div className="mt-0.5 tabular-nums">{(cmp.nav_deviation.max_nav_dev * 100).toFixed(2)}%</div>
                </div>
                <div className="px-4">
                  <div className="text-xs text-ink-faint">净值相关性</div>
                  <div className="mt-0.5 tabular-nums">{cmp.nav_deviation.corr ?? '—'}</div>
                </div>
                <div className="px-4">
                  <div className="text-xs text-ink-faint">超阈天数</div>
                  <div className="mt-0.5 tabular-nums">{cmp.nav_deviation.n_mismatch_days}</div>
                </div>
                <div className="px-4">
                  <div className="text-xs text-ink-faint">成交匹配率</div>
                  <div className="mt-0.5 tabular-nums">{(cmp.trade_comparison.match_rate * 100).toFixed(0)}%</div>
                </div>
              </div>
            </Panel>
          )}
        </>
      )}
    </div>
  );
}
