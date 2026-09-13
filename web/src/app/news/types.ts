/** 资讯模块类型：与后端 /api/news 契约（Task 6）对齐。 */

export type NewsSourceKind = 'telegraph' | 'news' | 'social' | 'report';

/** GET /news/items 单条：字段与 query_news 返回一致 */
export type NewsItemDTO = {
  news_id: string;
  source: string;
  source_name: string | null;
  external_id: string;
  title: string | null;
  content: string | null;
  url: string | null;
  industry_code: string | null;
  symbols: string[] | null;
  published_at: string | null;
  collected_at: string | null;
  quality_flags: number | null;
  source_tag: string | null;
};

/** GET /news/items 返回 { total, items } */
export type ItemsPage = {
  total: number;
  items: NewsItemDTO[];
};

/** GET /industries 单行：计数 + news.yaml 中文名 */
export type IndustryStat = {
  industry_code: string;
  count: number;
  industry_name?: string | null;
};

/** GET /sources 单行：注册表 + 库内统计并集 */
export type SourceStat = {
  source: string;
  category: NewsSourceKind | null;
  source_name?: string | null;
  count: number;
  last_collected_at: string | null;
};

/** GET /tasks 单行 */
export type NewsTaskRow = {
  task_id: string;
  kind: string;
  params: Record<string, unknown>;
  status: string;
  sources_status: Record<string, unknown>;
  rows_written: number | null;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
};

/** POST /tasks 创建/重试的返回（execute_task 结果） */
export type NewsTaskResult = {
  task_id: string;
  status: string;
  rows_written?: number | null;
};

/** fetchItems 过滤条件（undefined 字段不进 query string） */
export type ItemFilter = {
  source?: string;
  industry?: string;
  symbol?: string;
  day?: string;
  keyword?: string;
  limit?: number;
  offset?: number;
  /** 前端重查触发器：仅参与依赖比较，不发给后端（采集完成后 +1 刷新列表） */
  reload?: number;
};
