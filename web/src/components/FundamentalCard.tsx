// 基本面评分卡 —— 数据源 /fundamental/score（行业相对分位 + 覆盖率）。
// 关键展示原则：总分必须与覆盖率一起出现。只匹配到 3 个指标就拿 90 分，
// 和 17 个指标全匹配拿 90 分，可信度完全不同。
'use client';

import useSWR from 'swr';
import { Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { fetcher } from '@/lib/api';

export type FundScoreRow = {
  symbol: string;
  industry: string | null;
  n_scored: number;
  n_metrics: number;
  coverage: number;
  raw_score: number;
  available_max: number;
  normalized_score: number;
  rating: string;
};

export type FundDetailItem = {
  item: string;
  label: string;
  module: string;
  value: number;
  p25: number;
  p50: number;
  p75: number;
  n: number;
  ratio: number;
  points: number;
  max_score: number;
};

export type FundScore = {
  symbol: string;
  asof: string;
  available: boolean;
  hint?: string;
  score: FundScoreRow | null;
  items: FundDetailItem[];
};

const MODULES: { key: string; label: string; max: number }[] = [
  { key: 'score_profitability', label: '盈利能力', max: 25 },
  { key: 'score_cashflow', label: '现金质量', max: 20 },
  { key: 'score_efficiency', label: '营运效率', max: 15 },
  { key: 'score_solvency', label: '偿债能力', max: 20 },
  { key: 'score_valuation', label: '估值水平', max: 20 },
];

const RATING_TONE: Record<string, string> = {
  优秀: 'text-up',
  良好: 'text-indigo',
  一般: 'text-ink',
  较差: 'text-down',
};

export function useFundamentalScore(symbol: string) {
  return useSWR<FundScore>(
    symbol ? `/fundamental/score?symbol=${encodeURIComponent(symbol)}` : null,
    fetcher,
  );
}

/** 分位位置条：显示当前值在 [P25, P75] 区间中的相对位置。 */
function BandBar({ item }: { item: FundDetailItem }) {
  const lo = item.p25;
  const hi = item.p75;
  const span = hi - lo;
  // 区间退化时（同行业取值相同）不给误导性的位置条
  if (!Number.isFinite(span) || Math.abs(span) < 1e-12) {
    return <span className="text-xs text-ink-faint">区间退化</span>;
  }
  const pct = Math.max(0, Math.min(1, (item.value - lo) / span));
  return (
    <span className="relative inline-block h-1.5 w-24 rounded bg-line align-middle">
      <span className="absolute inset-y-0 left-1/2 w-px bg-ink-faint" />
      <span
        className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-indigo"
        style={{ left: `${pct * 100}%` }}
      />
    </span>
  );
}

export default function FundamentalCard({ symbol }: { symbol: string }) {
  const { data, isLoading, error } = useFundamentalScore(symbol);

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>基本面加载失败：{String(error)}</ErrorNote>;
  if (!data) return null;
  if (!data.available || !data.score) {
    return <Empty>{data.hint ?? '暂无基本面数据'}</Empty>;
  }

  const s = data.score;
  const row = s as unknown as Record<string, number>;
  const coveragePct = Math.round(s.coverage * 100);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-end gap-4">
          <Stat
            label="基本面评分"
            value={<span className="font-song text-3xl font-semibold tabular-nums">
              {s.normalized_score.toFixed(1)}
            </span>}
            tone={RATING_TONE[s.rating] ?? 'text-ink'}
          />
          <span className={`pb-1 text-sm ${RATING_TONE[s.rating] ?? 'text-ink'}`}>
            {s.rating}
          </span>
        </div>
        <div className="grid grid-cols-3 gap-x-8">
          <Stat label="行业" value={s.industry ?? '—'} />
          <Stat label="指标覆盖" value={`${coveragePct}%（${s.n_scored}/${s.n_metrics}）`} />
          <Stat label="观察日" value={data.asof} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-y-3 md:grid-cols-5">
        {MODULES.map((m) => (
          <Stat
            key={m.key}
            label={`${m.label} / ${m.max}`}
            value={<span className="tabular-nums">
              {(row[m.key] ?? 0).toFixed(1)}
            </span>}
          />
        ))}
      </div>

      {coveragePct < 80 && (
        <p className="text-xs text-gold">
          覆盖率偏低：总分只由已匹配到的指标算出，横向比较时请优先看覆盖度相近的标的。
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="table-dense">
          <thead>
            <tr>
              <th className="text-left">指标</th>
              <th className="text-right">本值</th>
              <th className="text-center">行业位置（P25–P75）</th>
              <th className="text-right">得分</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((it) => (
              <tr key={it.item} className="hover:bg-white">
                <td>
                  {it.label}
                  <span className="ml-1 font-mono text-xs text-ink-faint">{it.item}</span>
                </td>
                <td className="text-right tabular-nums">{it.value.toFixed(2)}</td>
                <td className="text-center">
                  <BandBar item={it} />
                  <span className="ml-2 text-xs tabular-nums text-ink-faint">
                    {it.p25.toFixed(1)} / {it.p50.toFixed(1)} / {it.p75.toFixed(1)}
                    <span className="ml-1">(n={it.n})</span>
                  </span>
                </td>
                <td className="text-right tabular-nums">
                  {it.points.toFixed(2)}
                  <span className="text-ink-faint"> / {it.max_score}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
