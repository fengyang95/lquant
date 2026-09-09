import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, get, getData, post } from './api';

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api 请求封装', () => {
  it('get 成功时返回 JSON', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { hello: 'world' }));
    await expect(get('/data/ping')).resolves.toEqual({ hello: 'world' });
    expect(fetch).toHaveBeenCalledWith('/api/data/ping', expect.anything());
  });

  it('后端 detail 透出为 ApiError 消息（比裸状态码有用）', async () => {
    vi.stubGlobal('fetch', mockFetch(404, { detail: '600519.SH 不在自选' }));
    const err = (await get('/watchlist/600519.SH').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe('600519.SH 不在自选');
  });

  it('非 JSON 错误体退回默认消息', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response('Internal Server Error', { status: 500 }),
    ));
    const err = (await get('/factors').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe('500 /factors');
  });

  describe('getData 封套取数（新老契约共存）', () => {
    it('封套成功解出 data', async () => {
      vi.stubGlobal('fetch', mockFetch(200, {
        code: 0, data: { value: 'weekly' }, message: 'ok', trace_id: 'abc',
      }));
      await expect(getData('/settings/key')).resolves.toEqual({ value: 'weekly' });
    });

    it('old-style 裸返回原样透传', async () => {
      vi.stubGlobal('fetch', mockFetch(200, { hello: 'world' }));
      await expect(getData('/data/ping')).resolves.toEqual({ hello: 'world' });
    });

    it('封套业务失败（code!=0）抛 ApiError 带 message', async () => {
      vi.stubGlobal('fetch', mockFetch(200, { code: 1, data: null, message: '取值越界' }));
      const err = await getData('/settings/nope').catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.message).toBe('取值越界');
    });

    it('HTTP 错误优先取封套 message，其次 detail', async () => {
      vi.stubGlobal('fetch', mockFetch(404, { code: 1, message: '无此 ETF' }));
      const err = await getData('/etf/by-symbol/999999').catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.message).toBe('无此 ETF');
    });
  });

  it('post 带方法与 JSON 头', async () => {
    const f = mockFetch(200, { added: '600519.SH' });
    vi.stubGlobal('fetch', f);
    await post('/watchlist', { symbol: '600519' });
    const [, init] = f.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual({ symbol: '600519' });
  });
});
