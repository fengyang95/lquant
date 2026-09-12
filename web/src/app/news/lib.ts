/** /api/news 前端封装：GET 用 getData 解封套，POST 用 postData。 */
import { getData, postData } from '@/lib/api';
import type { IndustryStat, ItemFilter, ItemsPage, NewsTaskResult, NewsTaskRow, SourceStat } from './types';

/**
 * 快源集合：每条来源单次请求即可取回，界面「采集快讯」用。
 * 不含逐股循环的 em_news（个股新闻）与 em_research（个股研报）—— 那两个要按
 * 成交额活跃池逐股请求，耗时数十秒到数分钟，走「全量采集」或定时任务。
 */
export const FAST_SOURCES = [
  'em_global',
  'ths_global',
  'futu_global',
  'cls_telegraph',
  'sina_7x24',
  'em_cjzc',
  'cctv_news',
  'em_notice',
  'baidu_hot',
] as const;

/** 过滤对象 → query string（undefined/空串字段丢弃；reload 只用于前端触发重查） */
export function itemsQuery(f: ItemFilter = {}): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (k === 'reload') continue;
    if (v !== undefined && v !== '') p.set(k, String(v));
  }
  return p.toString();
}

export async function fetchItems(f: ItemFilter = {}): Promise<ItemsPage> {
  return getData<ItemsPage>(`/news/items?${itemsQuery(f)}`);
}

export function fetchIndustries(): Promise<IndustryStat[]> {
  return getData<IndustryStat[]>('/news/industries');
}

export function fetchSources(): Promise<SourceStat[]> {
  return getData<SourceStat[]>('/news/sources');
}

export function fetchTasks(limit = 20): Promise<NewsTaskRow[]> {
  return getData<NewsTaskRow[]>(`/news/tasks?limit=${limit}`);
}

/** 创建并同步执行采集任务（manual；可选 date/sources） */
export function triggerTask(body: { kind?: string; date?: string; sources?: string[] } = {}): Promise<NewsTaskResult> {
  return postData<NewsTaskResult>('/news/tasks', { kind: 'manual', ...body });
}

/** 重试任务中失败的 source */
export function retryTask(taskId: string): Promise<NewsTaskResult> {
  return postData<NewsTaskResult>(`/news/tasks/${encodeURIComponent(taskId)}/retry`, {});
}
