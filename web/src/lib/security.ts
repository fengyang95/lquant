/** 个股分析报告的类型契约与展示辅助。
 *
 * 与后端 `lquant.security.contract`（SCHEMA_VERSION）一一对应。
 * 后端契约一变就要同步这里 —— `schema_version` 会在页面上透出，便于排查。
 *
 * 通用部分（评分块 / 指标 / 角度 / 配色）在 `@/lib/report`，这里 re-export，
 * 老 import 路径不变。
 */
export type {
  AngleResult,
  Contribution,
  Metric,
  RiskBlock,
  ScoreBlock,
  Signal,
} from '@/lib/report';

export {
  pctText,
  scoreWidth,
  signalBg,
  signalLabel,
  signalTone,
} from '@/lib/report';

import type { AngleResult, Metric, RiskBlock, ScoreBlock } from '@/lib/report';

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

/** 校验响应体确实是本契约的报告（防上游返回 [] / 错误结构时页面崩掉）。 */
export function isSecurityAnalysis(v: unknown): v is SecurityAnalysis {
  if (!v || typeof v !== 'object') return false;
  const o = v as Record<string, unknown>;
  return typeof o.symbol === 'string'
    && Array.isArray(o.angles)
    && !!o.score && typeof o.score === 'object';
}
