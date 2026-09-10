import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchIndustries,
  fetchItems,
  fetchSources,
  fetchTasks,
  itemsQuery,
  retryTask,
  triggerTask,
} from '../lib';

/** 封套响应构造器 */
function envelope(data: unknown, code = 0) {
  return new Response(JSON.stringify({ code, data, message: code === 0 ? 'ok' : 'boom' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('itemsQuery', () => {
  it('undefined/空串丢弃，数值转字符串', () => {
    expect(itemsQuery({ industry: 'bank', limit: 50 })).toBe('industry=bank&limit=50');
    expect(itemsQuery({ source: undefined, keyword: '' })).toBe('');
  });
});

describe('fetch* GET 封装', () => {
  it('fetchItems 拼 query 并解封套 { total, items }', async () => {
    const spy = vi.fn().mockResolvedValue(envelope({ total: 1, items: [{ news_id: 'n1' }] }));
    vi.stubGlobal('fetch', spy);
    const page = await fetchItems({ industry: 'bank', limit: 50, offset: 50 });
    const url = spy.mock.calls[0][0] as string;
    expect(url).toContain('/api/news/items?');
    expect(url).toContain('industry=bank');
    expect(url).toContain('limit=50');
    expect(url).toContain('offset=50');
    expect(page.total).toBe(1);
    expect(page.items[0].news_id).toBe('n1');
  });

  it('fetchItems 无过滤 → /api/news/items? 无多余参数', async () => {
    const spy = vi.fn().mockResolvedValue(envelope({ total: 0, items: [] }));
    vi.stubGlobal('fetch', spy);
    await fetchItems();
    expect(spy.mock.calls[0][0]).toBe('/api/news/items?');
  });

  it('fetchIndustries/fetchSources/fetchTasks 打到对应端点', async () => {
    const spy = vi.fn().mockImplementation(async () => envelope([]));
    vi.stubGlobal('fetch', spy);
    await fetchIndustries();
    await fetchSources();
    await fetchTasks(5);
    const urls = spy.mock.calls.map((c: unknown[]) => c[0] as string);
    expect(urls).toContain('/api/news/industries');
    expect(urls).toContain('/api/news/sources');
    expect(urls).toContain('/api/news/tasks?limit=5');
  });

  it('code != 0 → 抛 ApiError(message)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(null, 1)));
    await expect(fetchItems()).rejects.toThrow('boom');
  });
});

describe('triggerTask / retryTask POST 封装', () => {
  it('triggerTask POST JSON 到 /news/tasks，默认 kind=manual', async () => {
    const spy = vi.fn().mockResolvedValue(envelope({ task_id: 't1', status: 'ok' }));
    vi.stubGlobal('fetch', spy);
    const r = await triggerTask({ sources: ['cls_telegraph'] });
    const [url, init] = spy.mock.calls[0];
    expect(url).toBe('/api/news/tasks');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ kind: 'manual', sources: ['cls_telegraph'] });
    expect(r.task_id).toBe('t1');
  });

  it('retryTask POST 到 /news/tasks/{id}/retry', async () => {
    const spy = vi.fn().mockResolvedValue(envelope({ task_id: 't1', status: 'ok' }));
    vi.stubGlobal('fetch', spy);
    await retryTask('t1');
    const [url, init] = spy.mock.calls[0];
    expect(url).toBe('/api/news/tasks/t1/retry');
    expect(init.method).toBe('POST');
  });
});
