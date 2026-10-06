/** 个股分析报告的类型契约与展示辅助。
 *
 * 与后端 `lquant.security.contract`（SCHEMA_VERSION）一一对应。
 * 后端契约一变就要同步这里 —— `schema_version` 会在页面上透出，便于排查。
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

export type Overview = {
  symbol: string;
  name: string | null;
  asof: string;
  sec_type: string | null;
  board: string | null;
  is_st: boolean;
  industry: string | null;
  industry_code: string | null;
  peer_count: number;
  benchmark: string | null;
  metrics: Metric[];
  notes: string[];
};

export type SecurityAnalysis = {
  schema_version: string;
  symbol: string;
  asof: string;
  overview: Overview;
  score: ScoreBlock;
  verdict: { points: string[]; risks: string[] };
  angles: AngleResult[];
  risk: RiskBlock;
  disclaimer: string;
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

/** 校验响应体确实是本契约的报告（防上游返回 [] / 错误结构时页面崩掉）。 */
export function isSecurityAnalysis(v: unknown): v is SecurityAnalysis {
  if (!v || typeof v !== 'object') return false;
  const o = v as Record<string, unknown>;
  return typeof o.symbol === 'string'
    && Array.isArray(o.angles)
    && !!o.score && typeof o.score === 'object';
}
