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

/** 数据版本（GET /data/version/latest、/data/versions） */
export type DataVersion = {
  dataset: string;
  version: string;
  created_at: string;
};

/** 质量问题历史（GET /data/issues，与 /data/crosscheck/issues 同源）。
 *  后端 query_issues 输出 issue_id + detail 内嵌 message，不是 id/message。 */
export type DataIssue = {
  issue_id: string;
  rule_code: string;
  dataset: string;
  severity: string; // fatal/error/warn/info
  symbol: string | null;
  trade_date: string | null;
  /** 后端 JSON 明细，结构随 rule 而异；message 为人类可读摘要 */
  detail: ({ message?: string } & Record<string, unknown>) | null;
  count: number;
  resolved: boolean;
  created_at: string;
};

/** 数据清理（POST /data/purge） */
export type PurgeResult = {
  dry_run: boolean;
  scope?: string;
  rows_matched: number;
  files_scanned?: number;
  files?: { file: string; rows_matched: number }[];
};

/** 数据字典（GET /data/dictionary） */
export type DictionaryTable = {
  table: string;
  description: string;
  fields: { name: string; type: string; description: string }[];
};
export type Dictionary = { tables: DictionaryTable[] };

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

/** 数据质量 issue（GET /data/crosscheck/issues）。
 *  字段级对拍明细来自 detail（Issue.extra），CROSS_SRC_DIFF 类 issue 才有 */
export type QualityIssue = {
  issue_id: string;
  dataset: string;
  symbol: string | null;
  trade_date: string | null;
  rule_code: string;
  severity: string;
  /** 后端存 JSON（如 {"message": "..."}），CROSS_SRC_DIFF 附带 field/primary/peer/deviation_pct/level */
  detail: {
    message?: string;
    field?: string;
    primary?: number | string | null;
    peer?: number | string | null;
    deviation_pct?: number | null;
    level?: string;
  } & Record<string, unknown>;
  count: number;
  resolved: boolean;
  created_at: string;
};
