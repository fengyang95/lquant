/** 分析报告契约的**通用前端类型与展示辅助**。
 *
 * 后端对应 `lquant.core.report`：个股分析（`lquant.security`）与行业分析
 * （`lquant.industry`）产出同一种报告。评分映射、指标形状、多空配色这些
 * 口径只在这里定义一次 —— 两套前端各写一份配色，迟早会出现「同一个 60 分
 * 在一个页面是红、在另一个页面是灰」。
 *
 * 各分析域自己的字段（symbol / industry …）由各自的 lib 补充。
 */

export type Signal = 'bullish' | 'bearish' | 'neutral';

export type Metric = {
  key: string;
  label: string;
  value: number | null;
  display: string | null;
  unit: string | null;
  percentile: number | null;
  signal: Signal | null;
  note: string | null;
};

export type AngleResult = {
  id: string;
  label: string;
  weight: number;
  desc: string;
  available: boolean;
  score: number | null;
  stance: Signal | null;
  coverage: number;
  summary: string;
  metrics: Metric[];
  hint: string | null;
  extra: Record<string, unknown>;
};

export type Contribution = {
  id: string;
  label: string;
  score: number;
  stance: Signal | null;
  weight: number;
  effective_weight: number;
  contribution: number;
};

export type ScoreBlock = {
  score: number | null;
  grade: string;
  stance: Signal | null;
  n_scored: number;
  n_angles: number;
  angle_coverage: number;
  data_coverage: number;
  weights: Record<string, number>;
  contributions: Contribution[];
  unscored?: string[];
};

export type RiskBlock = {
  available: boolean;
  title?: string;
  scored?: boolean;
  metrics: Metric[];
  flags: string[];
  hint: string | null;
};

/** 多空配色：A 股口径**红涨绿跌**（`up` 是朱砂、`down` 是青绿）。 */
export function signalTone(signal?: Signal | null): string {
  if (signal === 'bullish') return 'text-up';
  if (signal === 'bearish') return 'text-down';
  return 'text-ink-dim';
}

export function signalLabel(signal?: Signal | null): string {
  if (signal === 'bullish') return '偏多';
  if (signal === 'bearish') return '偏空';
  if (signal === 'neutral') return '中性';
  return '—';
}

export function signalBg(signal?: Signal | null): string {
  if (signal === 'bullish') return 'bg-up text-white';
  if (signal === 'bearish') return 'bg-down text-white';
  return 'bg-line text-ink-dim';
}

/** 分数 → 进度条宽度（0–100）。 */
export function scoreWidth(score?: number | null): string {
  if (score == null) return '0%';
  return `${Math.max(0, Math.min(100, score))}%`;
}

/** 覆盖度 → 百分比文案。 */
export function pctText(v?: number | null, digits = 0): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** 结构自检：响应体确实是「角度报告」而不是 `[]` / 错误结构。 */
export function isAngleReport(v: unknown): v is { angles: AngleResult[]; score: ScoreBlock } {
  if (!v || typeof v !== 'object') return false;
  const o = v as Record<string, unknown>;
  return Array.isArray(o.angles) && !!o.score && typeof o.score === 'object';
}
