/** qlib 前端 API 封装（/api/qlib/*）。 */
import { get, post, postData, putData } from './api';

export const EXPORT_FIELDS: string[] = [
  'open', 'high', 'low', 'close', 'volume', 'amount', 'vwap', 'factor',
  'turnover_rate', 'total_mv', 'float_mv', 'pe_ttm', 'pb_mrq', 'ps_ttm', 'pct_chg',
];

export type QlibStatus = {
  dir: string;
  exists: boolean;
  manifest?: Record<string, unknown> | null;
  calendar?: { start: string; end: string; days: number } | null;
};

export type QlibConfigMeta = { name: string; path: string; mtime: number };

export type QlibRunStatus =
  'queued' | 'running' | 'finished' | 'failed' | 'canceled';

export type QlibRun = {
  id: string;
  config: string;
  market: string | null;
  exp_name: string;
  status: QlibRunStatus;
  metrics: Record<string, number> | null;
  config_snapshot: string | null;
  log_path: string | null;
  log_tail?: string | null;
  created_at: string;
  finished_at: string | null;
  error: string | null;
};

export type CompareRow = { metric: string } & Record<string, string | number | null>;

export type ExportIn = {
  start?: string | null;
  end?: string | null;
  symbols?: string[] | null;
  sec_types?: string[] | null;
  fields?: string[] | null;
  top?: number | null;
};

export function getQlibStatus(): Promise<QlibStatus> {
  return get<QlibStatus>('/qlib/status');
}

export function postQlibExport(body: ExportIn): Promise<{ job_id: string }> {
  return postData<{ job_id: string }>('/qlib/export', body);
}

export function listQlibConfigs(): Promise<QlibConfigMeta[]> {
  return get<QlibConfigMeta[]>('/qlib/configs');
}

export function getQlibConfig(name: string): Promise<{ name: string; content: string }> {
  return get(`/qlib/configs/${encodeURIComponent(name)}`);
}

export function putQlibConfig(name: string, content: string): Promise<{ saved: boolean }> {
  return putData(`/qlib/configs/${encodeURIComponent(name)}`, { content });
}

export function postQlibWorkflow(body: {
  config: string; market?: string | null; exp_name: string;
}): Promise<{ run_id: string }> {
  return postData('/qlib/workflow', body);
}

export function listQlibRuns(limit = 50): Promise<QlibRun[]> {
  return get<QlibRun[]>(`/qlib/runs?limit=${limit}`);
}

export function getQlibRun(id: string): Promise<QlibRun> {
  return get<QlibRun>(`/qlib/runs/${encodeURIComponent(id)}`);
}

export function cancelQlibRun(id: string): Promise<{ canceled: boolean }> {
  return post(`/qlib/runs/${encodeURIComponent(id)}/cancel`, {});
}

export function compareQlibRuns(ids: [string, string]) {
  return postData('/qlib/runs/compare', { ids });
}