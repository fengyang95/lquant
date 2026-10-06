// 基本面评分卡 —— 数据源 /fundamental/score（行业相对分位 + 覆盖率）。
// 关键展示原则：总分必须与覆盖率一起出现。只匹配到 3 个指标就拿 90 分，
// 和 17 个指标全匹配拿 90 分，可信度完全不同。
//
// 模块口径**必须**从 /fundamental/metrics 取，不能在前端硬编码模块名与满分：
// 这里原先写死了 盈利25/现金20/效率15/偿债20/估值20，与后端 MODULE_WEIGHTS
// 重复定义。后端一改权重（或摘掉/新增模块），评分卡就静默显示错的满分，
// 而同项目的排名页却用 /metrics 动态渲染 —— 两个页面对同一份分数给出不同口径。
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
  source?: string;
};

/** /fundamental/score 里每个模块的可得性（n_scored_rows=0 → 该模块本次没数据源） */
export type ModuleStat = {
  n_metrics: number;
  max_score: number;
  n_scored_rows: number;
  hit_rate: number;
};

export type FundScore = {
  symbol: string;
  asof: string;
  available: boolean;
  hint?: string;
  score: FundScoreRow | null;
  items: FundDetailItem[];
  modules?: Record<string, ModuleStat>;
  valuation?: Record<string, number | null> | null;
};

type ModuleMeta = { name: string; label: string; weight: number; n_metrics?: number };
type MetricsResp = { modules: ModuleMeta[]; metrics: unknown[] };

const RATING_TONE: Record<string, string> = {
  优秀: 'text-up',
  良好: 'text-indigo',
  一般: 'text-ink',
  较差: 'text-down',
};

/** 覆盖率低于此值就提示：总分只由部分指标算出，横向比较要同类比。 */
const LOW_COVERAGE = 0.8;

export function useFundamentalScore(symbol: string) {
  return useSWR<FundScore>(
    symbol ? `/fundamental/score?symbol=${encodeURIComponent(symbol)}` : null,
    fetcher,
  );
}

export function useFundamentalCatalogue() {
  return useSWR<MetricsResp>('/fundamental/metrics', fetcher);
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

/** 口径未加载时的兜底：从分数行的 score_* 键反推模块名。 */
function fallbackModules(row: Record<string, unknown>): ModuleMeta[] {
  return Object.keys(row)
    .filter((k) => k.startsWith('score_'))
    .map((k) => {
      const name = k.slice('score_'.length);
      return { name, label: name, weight: 0 };
    });
}

export default function FundamentalCard({ symbol }: { symbol: string }) {
  const { data, isLoading, error } = useFundamentalScore(symbol);
  const { data: catalogue } = useFundamentalCatalogue();

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>基本面加载失败：{String(error)}</ErrorNote>;
  if (!data) return null;
  if (!data.available || !data.score) {
    return <Empty>{data.hint ?? '暂无基本面数据'}</Empty>;
  }

  const s = data.score;
  const row = s as unknown as Record<string, unknown>;
  const coveragePct = Math.round(s.coverage * 100);
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const modules = catalogue?.modules?.length ? catalogue.modules : fallbackModules(row);

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
        {modules.map((m) => {
          const st = data.modules?.[m.name];
          const dead = st != null && st.n_scored_rows === 0;
          return (
            <Stat
              key={m.name}
              label={m.weight ? `${m.label} / ${m.weight}` : m.label}
              value={<span className={`tabular-nums ${dead ? 'text-ink-faint' : ''}`}>
                {num(row[`score_${m.name}`]).toFixed(1)}
              </span>}
              hint={dead ? '本期无数据源' : undefined}
            />
          );
        })}
      </div>

      {coveragePct < LOW_COVERAGE * 100 && (
        <p className="text-xs text-gold">
          覆盖率偏低：总分只由已匹配到的指标算出，横向比较时请优先看覆盖度相近的标的。
        </p>
      )}

      {data.valuation && Object.values(data.valuation).some((v) => v != null) && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-dim">
          <span className="text-ink-faint">日线估值原始值：</span>
          {data.valuation.pe_ttm != null && (
            <span>PE(TTM) {data.valuation.pe_ttm.toFixed(2)}</span>
          )}
          {data.valuation.pb != null && <span>PB {data.valuation.pb.toFixed(2)}</span>}
          {data.valuation.dividend_yield != null && (
            <span>股息率 {data.valuation.dividend_yield.toFixed(2)}%</span>
          )}
        </div>
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
                  {it.source === 'derived' && (
                    <span className="ml-1 text-xs text-gold" title="由原始报表科目现算">
                      派生
                    </span>
                  )}
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
