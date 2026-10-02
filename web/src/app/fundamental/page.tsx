// 全市场基本面排名 —— 数据源 /fundamental/scores（行业相对分位 + 覆盖率）。
// 排名用归一化分数，但把覆盖率单列出来：低覆盖的高分不具备可比的可信度。
'use client';

import { useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { get, post } from '@/lib/api';

type ScoreRow = {
  symbol: string;
  industry: string | null;
  n_scored: number;
  n_metrics: number;
  coverage: number;
  raw_score: number;
  available_max: number;
  normalized_score: number;
  rating: string;
  [key: string]: unknown;
};

type UniverseResp = {
  asof: string;
  available: boolean;
  hint?: string;
  n_scored: number;
  rows: ScoreRow[];
};

type ModuleMeta = { name: string; label: string; weight: number };
type MetricMeta = {
  item: string; label: string; module: string; max_score: number;
  higher_better: boolean; direction: string;
};
type MetricsResp = { modules: ModuleMeta[]; metrics: MetricMeta[] };

const RATING_TONE: Record<string, string> = {
  优秀: 'text-up',
  良好: 'text-indigo',
  一般: 'text-ink',
  较差: 'text-down',
};

export default function FundamentalPage() {
  const [asof, setAsof] = useState('');
  const [minCoverage, setMinCoverage] = useState(0.5);
  const [limit, setLimit] = useState(50);

  const { data: catalogue } = useSWR<MetricsResp>('/fundamental/metrics', get);

  const { data, isLoading, error } = useSWR<UniverseResp>(
    ['/fundamental/scores', asof, minCoverage, limit],
    () => post<UniverseResp>('/fundamental/scores', {
      asof: asof || null,
      min_coverage: minCoverage,
      limit,
    }),
  );

  const modules = catalogue?.modules ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="基本面排名"
        sub="行业相对分位评分（PIT：只用观察日已公告的报表）"
        actions={
          <Link href="/factors" className="btn">去因子页</Link>
        }
      />

      <Panel title="筛选">
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">观察日（留空=今天）</div>
            <input type="date" value={asof} onChange={(e) => setAsof(e.target.value)}
                   className="input input-mono" />
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">
              最低覆盖率 {(minCoverage * 100).toFixed(0)}%
            </div>
            <input type="range" min={0} max={1} step={0.05} value={minCoverage}
                   aria-label="最低覆盖率"
                   onChange={(e) => setMinCoverage(+e.target.value)}
                   className="w-40" />
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">条数</div>
            <select value={limit} onChange={(e) => setLimit(+e.target.value)} className="input">
              {[20, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        </div>
      </Panel>

      <Panel title="评分结果" meta={data?.asof ? `观察日 ${data.asof}` : ''}>
        {isLoading && <Loading />}
        {error && <ErrorNote>加载失败：{String(error)}</ErrorNote>}
        {!isLoading && !error && data && !data.available && (
          <Empty>{data.hint ?? '暂无数据'}</Empty>
        )}
        {!isLoading && !error && data?.available && (
          data.rows.length === 0 ? (
            <Empty>当前筛选条件下没有标的 —— 试试降低最低覆盖率</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="w-10 text-right">#</th>
                    <th className="text-left">标的</th>
                    <th className="text-left">行业</th>
                    <th className="text-right">归一化分</th>
                    <th className="text-right">覆盖率</th>
                    <th className="text-left">评级</th>
                    {modules.map((m) => (
                      <th key={m.name} className="text-right">{m.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r, i) => (
                    <tr key={r.symbol} className="hover:bg-white">
                      <td className="text-right tabular-nums text-ink-faint">{i + 1}</td>
                      <td className="font-mono">
                        <Link href={`/security/${r.symbol}`} className="text-indigo hover:underline">
                          {r.symbol}
                        </Link>
                      </td>
                      <td>{r.industry ?? '—'}</td>
                      <td className="text-right tabular-nums">
                        {r.normalized_score.toFixed(1)}
                      </td>
                      <td className="text-right tabular-nums text-ink-dim">
                        {(r.coverage * 100).toFixed(0)}%
                      </td>
                      <td className={`${RATING_TONE[r.rating] ?? 'text-ink'}`}>{r.rating}</td>
                      {modules.map((m) => (
                        <td key={m.name} className="text-right tabular-nums">
                          {Number(r[`score_${m.name}`] ?? 0).toFixed(1)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </Panel>

      <Panel title="评分口径" meta="模块权重合计 100">
        {!catalogue ? <Loading /> : (
          <>
            <div className="grid grid-cols-2 gap-y-3 md:grid-cols-5">
              {modules.map((m) => (
                <Stat key={m.name} label={m.label} value={
                  <span className="tabular-nums">{m.weight}</span>
                } />
              ))}
            </div>
            <div className="mt-4 overflow-x-auto">
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="text-left">指标</th>
                    <th className="text-left">模块</th>
                    <th className="text-right">满分</th>
                    <th className="text-left">方向</th>
                  </tr>
                </thead>
                <tbody>
                  {catalogue.metrics.map((m) => (
                    <tr key={m.item} className="hover:bg-white">
                      <td>
                        {m.label}
                        <span className="ml-1 font-mono text-xs text-ink-faint">{m.item}</span>
                      </td>
                      <td>{modules.find((x) => x.name === m.module)?.label ?? m.module}</td>
                      <td className="text-right tabular-nums">{m.max_score}</td>
                      <td className="text-ink-dim">{m.direction}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-ink-faint">
              每个指标按「同行业内的相对位置」给分：正向指标 ≥P75 满分，反向指标 ≤P25 满分。
              分位只用观察日已公告的报表计算，样本不足 5 个的行业不出分位（该指标不计分，覆盖率会下降）。
            </p>
          </>
        )}
      </Panel>
    </div>
  );
}
