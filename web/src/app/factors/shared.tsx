'use client';

/**
 * 因子页共享层：评价数据包（metrics / series）类型 + 可复用面板与图表 option。
 * page.tsx（快速评价）与 [name]/page.tsx（因子详情）共用同一口径，
 * 避免两处对同一份后端数据各自解释、逐渐漂移。
 */

import Chart from '@/components/Chart';
import { Panel } from '@/components/Panel';
import { Empty } from '@/components/States';
import { C, axes, legend, tooltip } from '@/lib/chart';

// —— 数据包类型（与后端 /factors/evaluate 返回口径一一对应） ——

export type TopNRow = {
  n: number;
  annual_return: number | null;
  annual_excess: number | null;
  excess_sharpe: number | null;
  max_drawdown: number | null;
  annual_turnover: number | null;
};

export type StyleRow = {
  style: string;
  kind: string;
  corr_mean: number | null;
  corr_abs_max: number | null;
  passed: boolean | null;
};

/** 预处理配方单步：op = stage，method = 注册表方法名，其余为方法参数 */
export type PreprocessStep = { op: string; method: string } & Record<string, unknown>;

export type RatingInfo = {
  rating: 'weak' | 'moderate' | 'strong';
  score: number;
  source: string;
  ic_mean: number | null;
  icir: number | null;
  t_stat_nw: number | null;
  t_threshold: number;
  significant: boolean;
  n_trials: number | null;
  monotonicity: number | null;
  ls_sharpe: number | null;
  reasons: string[];
  blockers: string[];
  thresholds: Record<string, number>;
};

export type RobustnessCheck = {
  name: string;
  status: 'passed' | 'failed' | 'skipped';
  value?: number | null;
  threshold?: number | null;
  hint?: string;
  detail?: unknown;
};

export type RobustnessInfo = {
  factor: string;
  checks: RobustnessCheck[];
  n_passed: number;
  n_judged: number;
  verdict: 'robust' | 'fragile' | 'unknown';
};

export type GroupIcRow = {
  group: string;
  ic_mean: number | null;
  rank_ic_mean: number | null;
  ir: number | null;
  n_days: number;
};

export type GroupIcInfo = {
  by: string | null;
  industry: GroupIcRow[];
  size: GroupIcRow[];
  size_col: string | null;
  error: string | null;
};

export type IndustryGroupQuantile = {
  n_groups: number;
  groups: { q: number; n: number; mean_ret: number }[];
  top_bottom_spread: number;
  monotonicity: number;
  n_obs: number;
  insufficient: boolean;
};

export type NeutralViewsInfo = {
  view: string;
  return_neutral_ic?: number | null;
  industry_group_quantile?: IndustryGroupQuantile | null;
  error?: string;
};

export type NeutralLadderRow = {
  label: string;
  covs: string[];
  ic_mean: number | null;
  rank_ic_mean: number | null;
  n_days: number;
  coverage?: number;
};

export type EvalErrors = Record<string, string>;

export type EvalSeries = {
  factor?: string;
  formula?: string;
  n_groups: number;
  n_samples: number;
  ic: { dates: string[]; ic: (number | null)[]; rank_ic: (number | null)[]; cum_ic: number[] };
  quantile: {
    dates: string[];
    curves: Record<string, (number | null)[]>;
    groups: { q: number; annual_return: number | null; sharpe: number | null; mean_ret: number | null }[];
    monotonicity: number | null;
  };
  decay: { horizons: number[]; ic: (number | null)[]; rank_ic: (number | null)[] };
  ic_by_year: { year: number; ic_mean: number | null; ir: number | null; positive_rate: number | null }[];
  neutral_ladder?: NeutralLadderRow[];
  neutral_views?: NeutralViewsInfo;
  group_ic?: GroupIcInfo;
  rolling?: {
    window: number;
    dates: string[];
    ic: (number | null)[];
    rank_ic: (number | null)[];
    ir: (number | null)[];
  };
  excess?: { dates: string[]; curves: Record<string, (number | null)[]>; benchmark: string };
  top_n?: TopNRow[];
  style_corr?: { styles?: StyleRow[]; threshold?: number; max_abs?: number | null; passed?: boolean | null };
  event_study?: {
    rel_periods: number[];
    curves: Record<string, (number | null)[]>;
    spread: (number | null)[];
    look_ahead_ratio: number | null;
    before: number; after: number; demeaned: boolean;
  };
  errors?: EvalErrors;
};

// —— 格式化 / 文案 ——

/** 分层净值用色：靛青系为主，多空单独朱砂 */
export const Q_COLORS = ['#94989F', '#31589E', '#4E6E8E', '#3E8E7E', '#B08A3E', '#A85B4B', '#6B4F9E', '#C3352B', '#1E7C55', '#2F5D4E'];

function fmt(v: number | null | undefined, digits = 4): string {
  return v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits);
}

const STAGE_LABEL: Record<string, string> = {
  winsorize: '去极值',
  standardize: '标准化',
  neutralize: '中性化',
  orthogonalize: '正交化',
};

/** 后端 errors 的 key → 中文标签；neutral_ladder:xxx 这类带后缀的按前缀归并 */
const ERROR_LABEL: Record<string, string> = {
  covariates: '协变量构建',
  industry_classify: '行业分类读取',
  neutral_views: '中性化视图',
  neutral_ladder: '归因阶梯',
  group_ic: '分组 IC',
  top_n: 'Top-N 收缩',
  style_corr: '风格相关性',
  turnover: '换手率',
  event_study: '事件式分层',
};

function errorLabel(key: string): string {
  if (ERROR_LABEL[key]) return ERROR_LABEL[key];
  const [head, tail] = key.split(':', 2);
  if (ERROR_LABEL[head]) return tail ? `${ERROR_LABEL[head]} · ${tail}` : ERROR_LABEL[head];
  return key;
}

// —— 组件 ——

/** 计算失败横幅：必须与「样本不足」区分，否则算炸了会被当成没数据 */
export function ErrorBanner({ errors, title = '部分计算未完成' }: {
  errors?: EvalErrors | null; title?: string;
}) {
  const entries = Object.entries(errors ?? {});
  if (!entries.length) return null;
  return (
    <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      <div className="font-medium">⚠ {title}（这是计算失败，不是「样本不足」）</div>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
        {entries.map(([key, value]) => (
          <li key={key}>
            <span className="font-medium">{errorLabel(key)}</span>：{value}
          </li>
        ))}
      </ul>
    </div>
  );
}

const RATING_META: Record<RatingInfo['rating'], { label: string; cls: string }> = {
  strong: { label: '强', cls: 'border-up/40 bg-up/10 text-up' },
  moderate: { label: '中', cls: 'border-gold/40 bg-gold/10 text-gold' },
  weak: { label: '弱', cls: 'border-line-strong bg-ink-faint/10 text-ink-dim' },
};

/** 评级面板：强/中/弱 + 入选理由 + 未达标项 */
export function RatingPanel({ rating }: { rating?: RatingInfo | null }) {
  if (!rating) {
    return (
      <Panel title="评级" meta="L2 判据结论">
        <Empty>暂无评级数据（评价响应未包含 rating）</Empty>
      </Panel>
    );
  }
  const meta = RATING_META[rating.rating];
  return (
    <Panel title="评级" meta={`判据来源：${rating.source}`}>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <span aria-label={`评级 ${meta.label}`} className={`border px-2 py-0.5 text-sm font-semibold ${meta.cls}`}>{meta.label}</span>
          <span className="text-xs text-ink-faint">
            得分 {rating.score} · IC {fmt(rating.ic_mean)} · ICIR {fmt(rating.icir)}
            {' · '}NW t {fmt(rating.t_stat_nw, 2)}（门槛 {fmt(rating.t_threshold, 2)}）
            {' · '}{rating.significant ? '校正后显著' : '校正后不显著'}
            {rating.n_trials != null ? ` · n_trials=${rating.n_trials}` : ''}
          </span>
        </div>
        {rating.reasons.length > 0 && (
          <div>
            <div className="mb-1 text-xs text-ink-faint">入选理由</div>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-ink-dim">
              {rating.reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
        {rating.blockers.length > 0 && (
          <div>
            <div className="mb-1 text-xs text-ink-faint">未达标项</div>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-up">
              {rating.blockers.map((b) => <li key={b}>{b}</li>)}
            </ul>
          </div>
        )}
        {!rating.reasons.length && !rating.blockers.length && (
          <div className="text-xs text-ink-faint">无理由明细</div>
        )}
      </div>
    </Panel>
  );
}

const ROBUST_NAME: Record<string, string> = {
  param_sensitivity: '参数敏感性',
  time_stability: '时间稳定性',
  start_date_sensitivity: '起点敏感性',
  best_month_removal: '最佳月度剔除',
  oos_decay: '样本外衰减',
};

const ROBUST_STATUS: Record<RobustnessCheck['status'], { label: string; cls: string }> = {
  passed: { label: '通过', cls: 'text-down' },
  failed: { label: '未通过', cls: 'text-up' },
  skipped: { label: '跳过', cls: 'text-ink-faint' },
};

const VERDICT_META: Record<RobustnessInfo['verdict'], { label: string; cls: string }> = {
  robust: { label: '稳健', cls: 'text-down' },
  fragile: { label: '脆弱', cls: 'text-up' },
  unknown: { label: '未知（可判项不足）', cls: 'text-ink-faint' },
};

/** 稳健性面板：默认不跑，需在评价表单里勾选后重跑才有数据 */
export function RobustnessPanel({ robustness }: { robustness?: RobustnessInfo | null }) {
  if (!robustness) {
    return (
      <Panel title="稳健性" meta="可选（需显式开启）">
        <div className="text-xs text-ink-faint">
          稳健性为可选检验（窗口扰动 / 分段稳定 / 起点敏感 / 月度剔除 / OOS 衰减），
          计算较慢：勾选「稳健性检验」后再跑一次评价才会返回结果。
        </div>
      </Panel>
    );
  }
  const verdict = VERDICT_META[robustness.verdict];
  return (
    <Panel
      title="稳健性"
      meta={`${verdict.label} · ${robustness.n_passed}/${robustness.n_judged} 项通过`}
    >
      <table className="table-dense">
        <thead>
          <tr>
            <th className="text-left">检查项</th>
            <th className="text-left">状态</th>
            <th className="text-left">数值</th>
            <th className="text-left">阈值</th>
            <th className="text-left">提示</th>
          </tr>
        </thead>
        <tbody>
          {robustness.checks.map((c) => {
            const st = ROBUST_STATUS[c.status];
            return (
              <tr key={c.name}>
                <td className="font-medium">{ROBUST_NAME[c.name] ?? c.name}</td>
                <td className={st.cls}>{st.label}</td>
                <td className="font-mono">{fmt(c.value)}</td>
                <td className="font-mono">{c.threshold == null ? '—' : fmt(c.threshold)}</td>
                <td className="text-ink-dim">{c.hint ?? '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Panel>
  );
}

/** 当前实际生效的预处理配方（metrics.steps；null = 未显式传，走内置默认口径） */
export function RecipeSteps({ steps }: { steps?: PreprocessStep[] | null }) {
  if (!steps || !steps.length) {
    return (
      <div className="text-xs text-ink-faint">
        内置默认配方（未显式传 steps）：mad 去极值 → zscore 标准化 → 市值 · 行业中性化
      </div>
    );
  }
  return (
    <ol className="flex flex-wrap items-center gap-1 text-xs">
      {steps.map((s, i) => (
        <li key={`${s.op}.${s.method}.${i}`} className="flex items-center gap-1">
          {i > 0 && <span className="text-ink-faint">→</span>}
          <span className="tag tag-on">{STAGE_LABEL[s.op] ?? s.op} · {s.method}</span>
          <span className="text-ink-faint">{stepParams(s)}</span>
        </li>
      ))}
    </ol>
  );
}

function stepParams(step: PreprocessStep): string {
  const parts = Object.entries(step)
    .filter(([k]) => k !== 'op' && k !== 'method')
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('/') : String(v)}`);
  return parts.length ? `(${parts.join(', ')})` : '';
}

/** IC 归因阶梯：协变量逐段叠加后 IC 掉多少（coverage<80% 的段置灰） */
export function NeutralLadderPanel({ rows, returnNeutralIc }: {
  rows?: NeutralLadderRow[]; returnNeutralIc?: number | null;
}) {
  return (
    <Panel
      title="IC 归因阶梯"
      meta={returnNeutralIc != null
        ? `收益中性化 IC 对照 = ${returnNeutralIc.toFixed(4)}（口径：收益~协变量取残差）`
        : '原始 → +市值 → +行业 → +换手率（逐段叠加看 IC 掉多少）'}
    >
      {rows && rows.length > 0 ? (
        <div className="space-y-1.5 px-1 py-2 text-xs">
          {rows.map((l) => {
            const cov = l.coverage ?? 1;
            const dim = cov < 0.8;
            const first = rows[0].ic_mean ?? 0;
            const v = l.ic_mean ?? 0;
            const drop = first !== 0 ? ((first - v) / Math.abs(first) * 100).toFixed(0) : '0';
            const w = first !== 0 ? Math.min(Math.abs(v / first) * 100, 100) : 0;
            return (
              <div key={l.label}
                className={`flex items-center gap-2 rounded-sm px-1 ${dim ? 'bg-ink-faint/10 opacity-60' : ''}`}
                title={dim ? `协变量覆盖率 ${(cov * 100).toFixed(0)}% < 80%` : ''}>
                <span className="w-24 text-ink-dim">{l.label}</span>
                <div className="h-3 flex-1 rounded-sm bg-ink-faint/10">
                  <div className="h-3 rounded-sm" style={{ width: `${w}%`, background: 'var(--c-indigo, #31589E)' }} />
                </div>
                <span className="w-16 text-right font-mono">{v.toFixed(4)}</span>
                <span className="w-10 text-right text-ink-faint">↓{drop}%</span>
              </div>
            );
          })}
        </div>
      ) : <Empty>协变量数据不足</Empty>}
    </Panel>
  );
}

/** 中性化后风格相关性：残差 vs 风格因子，超阈值说明 alpha 可能是风格马甲 */
export function StyleCorrPanel({ styleCorr }: {
  styleCorr?: { styles?: StyleRow[]; threshold?: number; max_abs?: number | null; passed?: boolean | null };
}) {
  const styles = styleCorr?.styles ?? [];
  return (
    <Panel
      title="中性化后风格相关性"
      meta={styles.length > 0
        ? `市值+行业中性化残差 vs 风格 · 阈值 ${styleCorr?.threshold ?? 0.14}`
        : '市值+行业中性化残差 vs 风格'}
    >
      {styles.length > 0 ? (
        <div className="space-y-1.5 px-1 py-2 text-xs">
          {styles.map((s) => {
            const v = s.corr_abs_max;
            const ok = s.passed;
            return (
              <div key={s.style} className="flex items-center gap-2 rounded-sm px-1">
                <span className="w-32 truncate text-ink-dim" title={s.style}>
                  {s.style.replace(/^cov_/, '')}{s.kind.startsWith('cat') ? ' (eta)' : ''}
                </span>
                <div className="h-3 flex-1 rounded-sm bg-ink-faint/10">
                  {v != null && (
                    <div className="h-3 rounded-sm"
                      style={{ width: `${Math.min(v * 100, 100)}%`, background: ok ? 'var(--c-up, #1E7C55)' : 'var(--c-down, #C3352B)' }} />
                  )}
                </div>
                <span className="w-14 text-right font-mono">{v != null ? v.toFixed(3) : '—'}</span>
                <span className="w-8 text-right">{ok == null ? '—' : ok ? '✓' : '⚠'}</span>
              </div>
            );
          })}
          {styleCorr?.passed != null && (
            <div className={`px-1 pt-1 ${styleCorr.passed ? 'text-up' : 'text-gold'}`}>
              {styleCorr.passed
                ? '✓ 所有风格 max|ρ| 均在阈值内 —— 中性化后未偷风格暴露'
                : '⚠ 存在超阈值风格相关 —— alpha 可能是某个风格的马甲，继续加中性化或重设计'}
            </div>
          )}
        </div>
      ) : <Empty>协变量数据不足</Empty>}
    </Panel>
  );
}

/** 分组 IC：行业组 + 市值组，识破「信号只来自小市值 / 某一行业」 */
export function GroupIcSection({ groupIc }: { groupIc?: GroupIcInfo | null }) {
  if (!groupIc) return null;
  const industry = groupIc.industry ?? [];
  const size = groupIc.size ?? [];
  const hasRows = industry.length > 0 || size.length > 0;
  return (
    <Panel
      title="分组 IC"
      meta={groupIc.by
        ? `行业维度 ${groupIc.by}${groupIc.size_col ? ` · 市值维度 ${groupIc.size_col}` : ''}`
        : '行业组 / 市值组 IC'}
    >
      {groupIc.error ? (
        <div className="text-xs text-up">分组 IC 计算失败：{groupIc.error}</div>
      ) : !hasRows ? (
        <Empty>样本不足</Empty>
      ) : (
        <div className="space-y-4">
          {industry.length > 0 && <GroupIcTable title="行业组 IC" rows={industry} />}
          {size.length > 0 && <GroupIcTable title="市值组 IC" rows={size} />}
        </div>
      )}
    </Panel>
  );
}

function GroupIcTable({ title, rows }: { title: string; rows: GroupIcRow[] }) {
  return (
    <div>
      <div className="mb-1 text-xs text-ink-faint">{title}</div>
      <table className="table-dense">
        <thead>
          <tr>
            <th className="text-left">分组</th>
            <th className="text-left">IC 均值</th>
            <th className="text-left">RankIC 均值</th>
            <th className="text-left">IR</th>
            <th className="text-left">天数</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.group}>
              <td className="font-medium">{r.group}</td>
              <td className={`font-mono ${(r.ic_mean ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>{fmt(r.ic_mean)}</td>
              <td className="font-mono">{fmt(r.rank_ic_mean)}</td>
              <td className="font-mono">{fmt(r.ir, 3)}</td>
              <td className="font-mono">{r.n_days}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 中性化视图：必须显式标明用的哪种口径 + 行业内分层的真实数字 */
export function NeutralViewsSection({ views }: { views?: NeutralViewsInfo | null }) {
  if (!views) {
    return (
      <Panel title="中性化视图" meta="三种中性化数学不等价">
        <Empty>中性化视图不可用</Empty>
      </Panel>
    );
  }
  const viewLabel = views.view && views.view !== 'error' ? views.view : null;
  const gq = views.industry_group_quantile;
  return (
    <Panel title="中性化视图" meta={viewLabel ? `口径：${viewLabel}` : '三种中性化数学不等价'}>
      <div className="space-y-3 text-xs">
        {viewLabel && (
          <div className="rounded-sm bg-ink-faint/10 px-2 py-1 text-ink-dim">
            当前中性化定义：{viewLabel}
          </div>
        )}
        {views.error && <div className="text-up">中性化视图计算失败：{views.error}</div>}
        <div>
          收益中性化 IC（对照视图）：
          <span className="ml-1 font-mono">{fmt(views.return_neutral_ic)}</span>
        </div>
        {gq && (
          gq.insufficient || !gq.groups.length ? (
            <div className="text-ink-faint">行业内分组：样本不足（n_obs={gq.n_obs}）</div>
          ) : (
            <div>
              <div className="mb-1 text-ink-faint">
                行业内分组（{gq.n_groups} 组 · 样本 {gq.n_obs}）
              </div>
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="text-left">组</th>
                    <th className="text-left">样本数</th>
                    <th className="text-left">平均收益</th>
                  </tr>
                </thead>
                <tbody>
                  {gq.groups.map((g) => (
                    <tr key={g.q}>
                      <td className="font-medium">Q{g.q}</td>
                      <td className="font-mono">{g.n}</td>
                      <td className={`font-mono ${g.mean_ret >= 0 ? 'text-up' : 'text-down'}`}>
                        {(g.mean_ret * 100).toFixed(3)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-1 text-ink-dim">
                首尾差 {fmt(gq.top_bottom_spread, 5)} · 单调性 {fmt(gq.monotonicity, 3)}
              </div>
            </div>
          )
        )}
      </div>
    </Panel>
  );
}

// —— 图表 option（page.tsx 与详情页共用同一份口径） ——

export function quantileNavOption(s: EvalSeries): object | null {
  if (!s.quantile.dates.length) return null;
  const { dates, curves } = s.quantile;
  const keys = Object.keys(curves).filter((k) => k !== 'long_short')
    .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  return {
    tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
    legend: legend({ top: 0, data: [...keys, '多空'] }),
    grid: { left: 48, right: 20, top: 30, bottom: 24 },
    dataZoom: [{ type: 'inside' as const }],
    ...axes({ data: dates }, { scale: true, name: '净值' }),
    series: [
      ...keys.map((k, i) => ({
        name: `第${k.slice(1)}组`, type: 'line' as const, data: curves[k],
        showSymbol: false, lineStyle: { width: 1, color: Q_COLORS[i % Q_COLORS.length] },
        itemStyle: { color: Q_COLORS[i % Q_COLORS.length] },
      })),
      {
        name: '多空', type: 'line' as const, data: curves.long_short, showSymbol: false,
        lineStyle: { width: 2.5, color: C.up }, itemStyle: { color: C.up }, z: 5,
      },
    ],
  };
}

export function groupReturnOption(s: EvalSeries): object | null {
  if (!s.quantile.groups.length) return null;
  const gs = s.quantile.groups;
  return {
    tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
    grid: { left: 56, right: 16, top: 24, bottom: 24 },
    ...axes({ data: gs.map((g) => `Q${g.q}`) },
      { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } }),
    series: [{
      name: '年化收益', type: 'bar' as const,
      data: gs.map((g) => ({
        value: g.annual_return,
        itemStyle: { color: (g.annual_return ?? 0) >= 0 ? C.up : C.down },
      })),
      barMaxWidth: 36,
    }],
  };
}

export function decayOption(s: EvalSeries): object | null {
  if (!s.decay.horizons.length) return null;
  const { horizons, ic, rank_ic } = s.decay;
  return {
    tooltip,
    legend: legend({ top: 0 }),
    grid: { left: 48, right: 20, top: 30, bottom: 24 },
    ...axes({ data: horizons.map((h) => `${h}天`) }, { name: 'IC均值' }),
    series: [
      { name: 'IC', type: 'line' as const, data: ic, showSymbol: true, lineStyle: { width: 1.8, color: C.up }, itemStyle: { color: C.up } },
      { name: 'RankIC', type: 'line' as const, data: rank_ic, showSymbol: true, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
    ],
  };
}

export function icByYearOption(s: EvalSeries): object | null {
  if (!s.ic_by_year.length) return null;
  const ys = s.ic_by_year;
  return {
    tooltip,
    grid: { left: 48, right: 16, top: 24, bottom: 24 },
    ...axes({ data: ys.map((y) => String(y.year)) }, { name: 'IC均值' }),
    series: [{
      name: '分年度IC', type: 'bar' as const,
      data: ys.map((y) => ({
        value: y.ic_mean,
        itemStyle: { color: (y.ic_mean ?? 0) >= 0 ? C.up : C.down },
      })),
      barMaxWidth: 32,
    }],
  };
}

/** 分层净值 / 分组年化两图并排（详情页直接复用） */
export function QuantileCharts({ series }: { series: EvalSeries }) {
  const nav = quantileNavOption(series);
  const grp = groupReturnOption(series);
  return (
    <>
      <Panel title="分层净值曲线" meta={`${series.n_groups} 组 + 多空`}>
        {nav ? <Chart option={nav} height={280} /> : <Empty>样本不足</Empty>}
      </Panel>
      <Panel title="分组年化收益" meta={`单调性 = ${series.quantile.monotonicity ?? '—'}`}>
        {grp ? <Chart option={grp} height={280} /> : <Empty>样本不足</Empty>}
      </Panel>
    </>
  );
}

export function excessNavOption(s: EvalSeries): object | null {
  const ex = s.excess;
  if (!ex?.dates.length) return null;
  const mk = (k: string, name: string, color: string, width: number) => ({
    name, type: 'line' as const, data: ex.curves[k] ?? [], showSymbol: false,
    lineStyle: { width, color }, itemStyle: { color },
  });
  return {
    tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
    legend: legend({ top: 0 }),
    grid: { left: 48, right: 20, top: 30, bottom: 24 },
    dataZoom: [{ type: 'inside' as const }],
    ...axes({ data: ex.dates }, { scale: true, name: '超额净值' }),
    series: [
      mk(`ex_q${s.n_groups}`, '最高组超额', C.up, 2.5),
      mk('ex_q1', '最低组超额', C.down, 1.5),
      mk('ex_long_short', '多空相对强弱', C.indigo, 1.5),
    ],
  };
}

export function eventStudyOption(s: EvalSeries): object | null {
  const es = s.event_study;
  if (!es?.rel_periods.length) return null;
  const keys = Object.keys(es.curves).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const zeroIdx = es.rel_periods.indexOf(0);
  return {
    tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
    legend: legend({ top: 0, data: [...keys.map((k) => `第${k.slice(1)}组`), '最高-最低'] }),
    grid: { left: 52, right: 20, top: 30, bottom: 24 },
    ...axes({ data: es.rel_periods.map((p) => `${p > 0 ? '+' : ''}${p}`) }, { scale: true, name: '累计收益' }),
    series: [
      ...keys.map((k, i) => ({
        name: `第${k.slice(1)}组`, type: 'line' as const, data: es.curves[k],
        showSymbol: false, lineStyle: { width: 1, color: Q_COLORS[i % Q_COLORS.length] },
        itemStyle: { color: Q_COLORS[i % Q_COLORS.length] },
        markLine: i === 0 && zeroIdx >= 0 ? {
          silent: true, symbol: 'none',
          lineStyle: { color: C.inkDim, type: 'dashed' as const },
          data: [{ xAxis: zeroIdx }],
        } : undefined,
      })),
      {
        name: '最高-最低', type: 'line' as const, data: es.spread, showSymbol: false,
        lineStyle: { width: 2.5, color: C.up }, itemStyle: { color: C.up }, z: 5,
      },
    ],
  };
}

/** Top-N 持仓收缩测试表 */
export function TopNTable({ rows }: { rows?: TopNRow[] | null }) {
  if (!rows || !rows.length) return null;
  return (
    <div className="mt-5">
      <div className="mb-2 text-xs text-ink-faint">
        Top-N 持仓收缩测试：头部集中通常收益不升、波动加大 —— 头部靠数量而非强度时会露馅
      </div>
      <table className="table-dense">
        <thead>
          <tr>
            <th className="text-left">持仓数</th>
            <th className="text-left">年化收益</th>
            <th className="text-left">年化超额</th>
            <th className="text-left">超额夏普</th>
            <th className="text-left">最大回撤</th>
            <th className="text-left">年化换手</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.n}>
              <td className="font-medium">Top {r.n}</td>
              <td className={`font-mono ${(r.annual_return ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                {r.annual_return != null ? `${(r.annual_return * 100).toFixed(2)}%` : '—'}
              </td>
              <td className={`font-mono ${(r.annual_excess ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                {r.annual_excess != null ? `${(r.annual_excess * 100).toFixed(2)}%` : '—'}
              </td>
              <td className="font-mono">{r.excess_sharpe ?? '—'}</td>
              <td className="font-mono text-down">
                {r.max_drawdown != null ? `${(r.max_drawdown * 100).toFixed(2)}%` : '—'}
              </td>
              <td className="font-mono">
                {r.annual_turnover != null ? `${(r.annual_turnover * 100).toFixed(0)}%` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
