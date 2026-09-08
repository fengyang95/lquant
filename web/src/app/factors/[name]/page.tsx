'use client';

/**
 * 因子详情 —— 「研报台」版式：定义 → 快速评价 → 历史报告。
 * 数据逻辑与旧版一致（SWR /factors/{name}、评价后 mutate 刷新 reports）。
 */

import { useState } from 'react';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
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

  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorNote>因子不存在或加载失败</ErrorNote>;

  return (
    <div className="space-y-5">
      <PageHeader
        title={data.name}
        sub={`注册于 ${data.created_at?.slice(0, 19)}`}
      />

      {/* 定义 */}
      <Panel title="定义">
        {data.expression ? (
          <code className="block bg-paper px-3 py-2 font-mono text-sm">{data.expression}</code>
        ) : (
          <div className="text-sm text-ink-faint">未登记 DSL 表达式（快速评价可用现算公式）</div>
        )}
        {data.description && <p className="mt-2 text-sm text-ink-dim">{data.description}</p>}
      </Panel>

      {/* 快速评价 */}
      <Panel title="快速评价" meta="基于数据湖日线现算，IC / 分层 / 衰减一次跑完">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <select
            value={formula}
            onChange={(e) => setFormula(e.target.value)}
            className="input"
          >
            {FORMULAS.map((f) => <option key={f}>{f}</option>)}
          </select>
          <button
            onClick={evaluate}
            disabled={busy}
            className="btn btn-accent"
          >
            {busy ? '评价中…' : '运行评价'}
          </button>
        </div>
        <Msg text={msg} />
        {res && (
          <div className={msg ? 'mt-4' : ''}>
            <div className="grid grid-cols-2 gap-y-4 divide-line border-t border-line pt-4 md:grid-cols-4 lg:grid-cols-6 md:divide-x">
              <div className="pr-4">
                <Stat label={`IC 均值（${res.formula}）`} value={res.ic.mean}
                  tone={res.ic.mean > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="ICIR" value={res.ic.ir} />
              </div>
              <div className="px-4">
                <Stat label="t 统计量" value={res.ic.t_stat} />
              </div>
              <div className="px-4">
                <Stat label="RankIC 均值" value={res.rank_ic_mean} />
              </div>
              <div className="px-4">
                <Stat label="多空年化" value={`${(res.long_short.annual_return * 100).toFixed(2)}%`}
                  tone={res.long_short.annual_return > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="多空夏普" value={res.long_short.sharpe} />
              </div>
              <div className="px-4 pt-4 md:pt-0 md:border-0 border-t border-line">
                <Stat label="多空最大回撤" value={`${(res.long_short.max_drawdown * 100).toFixed(2)}%`} tone="text-down" />
              </div>
              <div className="px-4">
                <Stat label="单调性" value={res.monotonicity} />
              </div>
              <div className="px-4">
                <Stat label="半衰期" value={res.half_life != null ? `${res.half_life} 天` : '—'} />
              </div>
              <div className="px-4">
                <Stat label="建议调仓" value={<span className="text-base">{res.suggested_rebalance}</span>} />
              </div>
              <div className="px-4">
                <Stat label="样本数" value={res.n_samples.toLocaleString()} />
              </div>
              <div className="px-4">
                <Stat label="完整报告" value={
                  <a href={res.report_url} target="_blank" className="font-sans text-sm text-indigo hover:underline" rel="noreferrer">
                    查看 ↗
                  </a>
                } />
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* 历史报告 */}
      <Panel title="历史报告" meta={`${data.reports.length} 份`}>
        {!data.reports.length ? (
          <Empty>暂无 —— 跑一次评价即生成</Empty>
        ) : (
          <ul className="divide-y divide-line text-sm">
            {data.reports.map((r) => (
              <li key={r.name} className="py-2">
                <a href={r.url} target="_blank" className="text-indigo hover:underline" rel="noreferrer">
                  {r.name} ↗
                </a>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
