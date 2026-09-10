import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useNewsFeed } from '../useNewsFeed';

/** 受控 Promise 工具：手动决定 resolve 顺序，模拟响应乱序到达 */
function deferred<T>(value: T) {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve, value };
}

function pageOf(ids: string[]) {
  const body = { code: 0, data: { total: 99, items: ids.map((news_id) => ({ news_id })) }, message: 'ok' };
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

describe('useNewsFeed 竞态守卫', () => {
  it('loadMore 在途时过滤条件变化：旧页响应被丢弃，不污染新过滤的列表', async () => {
    // 三个受控响应：初始页 a、loadMore 页 b、新过滤页 c
    const dA = deferred(pageOf(['a1', 'a2']));
    const dB = deferred(pageOf(['b1']));
    const dC = deferred(pageOf(['c1', 'c2']));

    const spy = vi.fn();
    spy.mockImplementationOnce(() => dA.promise);
    spy.mockImplementationOnce(() => dB.promise);
    spy.mockImplementationOnce(() => dC.promise);
    vi.stubGlobal('fetch', spy);

    const { result, rerender } = renderHook(({ kw }) => useNewsFeed({ keyword: kw }), {
      initialProps: { kw: 'a' },
    });

    // 初始页落地
    await act(async () => dA.resolve(dA.value));
    await waitFor(() => expect(result.current.items.map((i) => i.news_id)).toEqual(['a1', 'a2']));

    // loadMore 发出（页 b 在途）
    act(() => {
      result.current.loadMore();
    });

    // 过滤条件变化 → effect 重查（页 c 在途），seq 前进
    rerender({ kw: 'c' });

    // 先落地旧 loadMore（页 b），再落地新查询（页 c）
    await act(async () => dB.resolve(dB.value));
    await act(async () => dC.resolve(dC.value));

    await waitFor(() => {
      // 旧页 b 的 b1 不应混入；列表是干净的新查询结果
      expect(result.current.items.map((i) => i.news_id)).toEqual(['c1', 'c2']);
    });
    expect(result.current.error).toBe('');
  });

  it('loadMore 正常追加下一页（守卫不误伤）', async () => {
    const dA = deferred(pageOf(['a1']));
    const dB = deferred(pageOf(['a2']));
    const spy = vi.fn();
    spy.mockImplementationOnce(() => dA.promise);
    spy.mockImplementationOnce(() => dB.promise);
    vi.stubGlobal('fetch', spy);

    const { result } = renderHook(() => useNewsFeed({ keyword: 'a' }));
    await act(async () => dA.resolve(dA.value));
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    act(() => {
      result.current.loadMore();
    });
    await act(async () => dB.resolve(dB.value));
    await waitFor(() => expect(result.current.items.map((i) => i.news_id)).toEqual(['a1', 'a2']));
  });
});
