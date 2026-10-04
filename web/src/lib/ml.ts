/** ML 模型前端 API 封装（/api/ml/*）。 */
import { get, post } from './api';

export type MlStage = 'candidate' | 'staging' | 'production' | 'archived';

export type MlModelVersion = {
  name: string;
  version: number;
  run_id: string | null;
  stage: MlStage;
  artifact_path: string | null;
  processor_path: string | null;
  metrics: Record<string, unknown>;
  fit_window: Record<string, unknown>;
  note: string | null;
  created_at: string | null;
  promoted_at: string | null;
};

export type MlRun = {
  run_id: string;
  model: string | null;
  model_name: string | null;
  model_version: number | null;
  stage: MlStage | null;
  metrics: Record<string, unknown> | null;
  train_rows: number | null;
  test_rows: number | null;
  train_end: string | null;
  test_end: string | null;
  created_at: string | null;
  artifact_path: string | null;
};

export type MlStatus = {
  backends: string[];
  model_lines: number;
  versions: number;
  by_stage: Record<MlStage, number>;
  production: Record<string, { version: number; metric: number | null }>;
  signals: { name: string; days: number; last: string; version: number }[];
  model_dir: string;
};

export type MlFeatures = {
  columns: string[];
  alpha158: string[];
  alpha158_count: number;
  formulas: string[];
};

export type MlSignalPage = {
  name: string;
  rows: number;
  items: { trade_date: string; symbol: string; signal: number; model_version: number }[];
  versions: number[];
};

export const STAGE_TEXT: Record<MlStage, string> = {
  candidate: '候选',
  staging: '预发',
  production: '线上',
  archived: '归档',
};

export const STAGE_BADGE: Record<MlStage, string> = {
  candidate: 'border-line-strong bg-ink-faint/10 text-ink-dim',
  staging: 'border-gold/40 bg-gold/10 text-gold',
  production: 'border-up/40 bg-up/10 text-up',
  archived: 'border-line bg-panel text-ink-faint',
};

/** 从训练记录里取一个可展示的样本外指标（缺省 —）。 */
export function rankIcOf(metrics: Record<string, unknown> | null | undefined): number | null {
  if (!metrics) return null;
  const ml = metrics.ml as Record<string, unknown> | undefined;
  const v = (ml?.test_rank_ic_mean ?? metrics.test_rank_ic_mean) as unknown;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export const mlApi = {
  status: () => get<MlStatus>('/ml/status'),
  features: (universe = 'all') =>
    get<MlFeatures>(`/ml/features?universe=${encodeURIComponent(universe)}`),
  runs: (limit = 50) => get<MlRun[]>(`/ml/runs?limit=${limit}`),
  models: (name?: string, stage?: string) => {
    const q = new URLSearchParams();
    if (name) q.set('name', name);
    if (stage) q.set('stage', stage);
    const s = q.toString();
    return get<MlModelVersion[]>(`/ml/models${s ? `?${s}` : ''}`);
  },
  production: (name: string) =>
    get<MlModelVersion>(`/ml/models/${encodeURIComponent(name)}/production`),
  promote: (name: string, version: number, stage: MlStage, note?: string) =>
    post<MlModelVersion>(`/ml/models/${encodeURIComponent(name)}/${version}/promote`, {
      stage,
      note: note ?? null,
    }),
  rollback: (name: string) =>
    post<MlModelVersion>(`/ml/models/${encodeURIComponent(name)}/rollback`, {}),
  signals: (name: string, limit = 200) =>
    get<MlSignalPage>(`/ml/signals?name=${encodeURIComponent(name)}&limit=${limit}`),
  train: (body: Record<string, unknown>) => post<{ job_id: string }>('/ml/train', body),
  retrain: (body: Record<string, unknown>) => post<{ job_id: string }>('/ml/retrain', body),
};
