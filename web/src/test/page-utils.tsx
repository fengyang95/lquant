import type { ReactElement } from 'react';
import { render } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { vi } from 'vitest';

/**
 * 路由页测试共享工具：
 * - okJson：`get`/`post`（裸契约）的 200 响应
 * - okEnvelope：`getData`/`fetcherData`（封套契约 {code,data,message}）的 200 响应
 * - stubPageFetch：按 URL 子串路由到 payload，未命中返回 404
 * - renderPage：SWRConfig 隔离缓存后渲染页面
 */

export function okJson(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export function okEnvelope(data: unknown): Response {
  return okJson({ code: 0, data, message: 'ok' });
}

export type RouteTable = Record<string, unknown>;

/** 未命中路由返回 404（裸 detail，裸/封套 fetcher 都能走错误分支） */
export function stubPageFetch(routes: RouteTable, statusByRoute?: Record<string, number>) {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    for (const [frag, payload] of Object.entries(routes)) {
      if (url.includes(frag)) {
        const status = statusByRoute?.[frag] ?? 200;
        return okJson(payload, status);
      }
    }
    return okJson({ detail: 'not found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

export function renderPage(ui: ReactElement): { container: HTMLElement } {
  return render(<SWRConfig value={{ provider: () => new Map() }}>{ui}</SWRConfig>);
}
