'use client';

import { useState } from 'react';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import { fetcher, post } from '@/lib/api';

type FactorDetail = {
  name: string;
  expression: string;
  description: string;
  created_at: string;
  reports: { name: string; url: string }[];
};

type EvalResult = {
  factor: string;
  formula: string;
  n_samples: number;
  ic: { mean: number; ir: number; t_stat: number; positive_rate: number };
  rank_ic_mean: number;
  long_short: { annual_return: number; sharpe: number; max_drawdown: number };
  monotonicity: number;
  half_life: number | null;
  suggested_rebalance: string;
  report_url: string;
};

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20', 'turnover'];

function Metric({ label, value, color }: { label: string; value: React.ReactNode; color?: string }) {
  return (
    <div>
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`font-semibold tabular-nums ${color ?? ''}`}>{value}</div>
    </div>
  );
}

export default function FactorDetailPage() {
  const { name = '' } = useParams<{ name: string }>();
  const { data, error, isLoading, mutate } = useSWR<FactorDetail>(
    name ? `/factors/${name}` : null, fetcher,
  );
  const [formula, setFormula] = useState('pct_change_20');
  const [res, setRes] = useState<EvalResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  async function evaluate() {
    setBusy(true);
    setMsg('');
    try {
      const r = await post<EvalResult>('/factors/evaluate', { factor: name, formula });
      setRes(r);
      mutate(); // 评价后 reports 列表可能新增
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  if (isLoading) return <div className="py-20 text-center text-neutral-400">加载中…</div>;
  if (error || !data) return <div className="py-20 text-center text-red-500">因子不存在或加载失败</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">{data.name}</h1>
        <span className="text-sm text-neutral-400">注册于 {data.created_at?.slice(0, 19)}</span>
      </div>

      {/* 定义 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">定义</div>
        {data.expression ? (
          <code className="block rounded-md bg-neutral-50 px-3 py-2 font-mono text-sm">{data.expression}</code>
        ) : (
          <div className="text-sm text-neutral-400">未登记 DSL 表达式（快速评价可用现算公式）</div>
        )}
        {data.description && <p className="mt-2 text-sm text-neutral-500">{data.description}</p>}
      </div>

      {/* 快速评价 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium">快速评价</span>
          <select
            value={formula}
            onChange={(e) => setFormula(e.target.value)}
            className="rounded-md border px-2 py-1.5 text-sm"
          >
            {FORMULAS.map((f) => <option key={f}>{f}</option>)}
          </select>
          <button
            onClick={evaluate}
            disabled={busy}
            className="rounded-md bg-red-600 px-4 py-1.5 text-sm text-white hover:bg-red-500 disabled:opacity-40"
          >
            {busy ? '评价中…' : '运行评价'}
          </button>
          <span className="text-xs text-neutral-400">基于数据湖日线现算，IC / 分层 / 衰减一次跑完</span>
        </div>
        {msg && <div className="mb-3 text-sm">{msg}</div>}
        {res && (
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Metric label={`IC 均值（${res.formula}）`} value={res.ic.mean}
              color={res.ic.mean > 0 ? 'text-up' : 'text-down'} />
            <Metric label="ICIR" value={res.ic.ir} />
            <Metric label="t 统计量" value={res.ic.t_stat} />
            <Metric label="RankIC 均值" value={res.rank_ic_mean} />
            <Metric label="多空年化" value={`${(res.long_short.annual_return * 100).toFixed(2)}%`}
              color={res.long_short.annual_return > 0 ? 'text-up' : 'text-down'} />
            <Metric label="多空夏普" value={res.long_short.sharpe} />
            <Metric label="多空最大回撤" value={`${(res.long_short.max_drawdown * 100).toFixed(2)}%`}
              color="text-down" />
            <Metric label="单调性" value={res.monotonicity} />
            <Metric label="半衰期" value={res.half_life != null ? `${res.half_life} 天` : '—'} />
            <Metric label="建议调仓" value={res.suggested_rebalance} />
            <Metric label="样本数" value={res.n_samples.toLocaleString()} />
            <Metric label="完整报告" value={
              <a href={res.report_url} target="_blank" className="text-blue-600 hover:underline" rel="noreferrer">
                查看 ↗
              </a>
            } />
          </div>
        )}
      </div>

      {/* 历史报告 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">历史报告（{data.reports.length}）</div>
        {!data.reports.length ? (
          <div className="py-4 text-center text-sm text-neutral-400">暂无 —— 跑一次评价即生成</div>
        ) : (
          <ul className="divide-y text-sm">
            {data.reports.map((r) => (
              <li key={r.name} className="py-2">
                <a href={r.url} target="_blank" className="text-blue-600 hover:underline" rel="noreferrer">
                  {r.name} ↗
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
