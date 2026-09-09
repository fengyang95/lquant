/** 数据管理页（/data）共享类型 —— 与后端 src/lquant/server/api/data.py 契约对应 */

/** 数据任务（ingest/tasks.py _row_to_task） */
export type DataTask = {
  task_id: string;
  kind: string; // full_backfill | daily_update
  params: Record<string, unknown>;
  status: string; // pending/running/ok/partial/failed/interrupted
  phase: string | null;
  total_symbols: number | null;
  done_symbols: number | null;
  failed_symbols: string[];
  failed_detail: { symbol: string; reason: string }[];
  rows_written: number | null;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
};

/** 数据源（GET /settings/providers） */
export type Provider = {
  name: string;
  enabled: boolean;
  capability: string[];
  note: string | null;
};

/** 配置项（GET /settings，封套） */
export type Setting = {
  key: string;
  value: unknown;
  type: 'str' | 'bool' | 'enum' | 'list';
  source: 'default' | 'config' | 'runtime';
  label: string;
  choices: string[] | null;
};

/** 跨源对拍返回（POST /data/crosscheck） */
export type CrosscheckResult = {
  summary: Record<string, number>;
  issues: unknown[];
  flagged_rows: number;
};

/** 数据质量 issue（GET /data/crosscheck/issues，未持久化 primary/peer 原值与偏差%） */
export type QualityIssue = {
  issue_id: string;
  dataset: string;
  symbol: string | null;
  trade_date: string | null;
  rule_code: string;
  severity: string;
  detail: string;
  count: number;
  resolved: boolean;
  created_at: string;
};
