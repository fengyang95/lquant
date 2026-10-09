/** 行业分析报告的类型契约与展示辅助。
 *
 * 与后端 `lquant.industry.contract`（SCHEMA_VERSION）一一对应。
 * 通用部分（评分块 / 指标 / 角度 / 配色）在 `@/lib/report`。
 */
import type { AngleResult, RiskBlock, ScoreBlock } from '@/lib/report';

export type IndustryLeader = {
  symbol: string;
  name: string | null;
  ret20: number | null;
  close: number | null;
};

export type IndustryRank = {
  rank: number;
  n_industries: number;
  percentile: number;
  r20: number;
};

export type IndustryOverview = {
  industry: string;
  industry_code: string;
  std: string | null;
  asof: string;
  member_count: number;
  active_members: number | null;
  day_ret: number | null;
  rank: IndustryRank | null;
  leaders: IndustryLeader[];
  benchmark: string | null;
  notes: string[];
  n_angles: number;
};

export type IndustryAnalysis = {
  schema_version: string;
  industry: string;
  industry_code: string;
  std: string | null;
  asof: string;
  overview: IndustryOverview;
  score: ScoreBlock;
  verdict: { points: string[]; risks: string[] };
  angles: AngleResult[];
  risk: RiskBlock;
  disclaimer: string;
};

/** 轮动榜的一行。区间收益列名随请求窗口变化（`r20` / `r60` / `r250`…）。 */
export type RotationRow = {
  industry_code: string;
  industry_name: string;
  n_members: number;
  amount_20d: number | null;
  pe_median: number | null;
  pb_median: number | null;
  rank: number;
  percentile: number;
  [key: string]: unknown;
};

export type RotationResponse = {
  asof: string;
  std: string | null;
  window: number;
  rows: RotationRow[];
  notes?: string[];
};

export type IndustryListRow = {
  industry_code: string;
  industry_name: string;
  n_members: number;
  last_ret: number | null;
};

export type IndustryListResponse = {
  asof: string;
  std: string | null;
  industries: IndustryListRow[];
  notes?: string[];
};

/** 取某一行在给定窗口下的区间收益（列名 `r{window}`）。 */
export function rowReturn(row: RotationRow, window: number): number | null {
  const v = row[`r${window}`];
  return typeof v === 'number' ? v : null;
}

/** 校验响应体确实是本契约的报告（防上游返回错误结构时页面崩掉）。 */
export function isIndustryAnalysis(v: unknown): v is IndustryAnalysis {
  if (!v || typeof v !== 'object') return false;
  const o = v as Record<string, unknown>;
  return typeof o.industry === 'string'
    && Array.isArray(o.angles)
    && !!o.score && typeof o.score === 'object';
}
