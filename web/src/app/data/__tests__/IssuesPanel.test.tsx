import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import IssuesPanel from '../IssuesPanel';
import type { DataIssue } from '../types';

const issues: DataIssue[] = [
  {
    issue_id: 'i1',
    rule_code: 'CROSS_SRC_DIFF',
    dataset: 'daily',
    severity: 'fatal',
    symbol: '600000.SH',
    trade_date: '2026-09-12',
    detail: { message: 'close 与对拍源偏差 5.2%' },
    count: 1,
    resolved: false,
    created_at: '2026-09-12T14:00:00',
  },
  {
    issue_id: 'i2',
    rule_code: 'STALE_PRICE',
    dataset: 'daily',
    severity: 'warn',
    symbol: null,
    trade_date: null,
    detail: { message: '连续 5 日未更新' },
    count: 5,
    resolved: false,
    created_at: '2026-09-11T14:00:00',
  },
];

function renderPanel(
  handler?: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (handler) return handler(url, init);
    if (url.includes('/data/issues')) {
      return new Response(JSON.stringify(issues), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <IssuesPanel />
    </SWRConfig>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('IssuesPanel 质量问题历史', () => {
  it('列表：severity 标签 / 规则 / 说明渲染', async () => {
    renderPanel();
    expect(await screen.findByText('CROSS_SRC_DIFF')).toBeInTheDocument();
    expect(screen.getByText('STALE_PRICE')).toBeInTheDocument();
    expect(screen.getByText('close 与对拍源偏差 5.2%')).toBeInTheDocument();
    // severity 标签（option 里也有同名文本，用 getAllByText 断言标签存在）
    expect(screen.getAllByText('fatal').length).toBeGreaterThan(1);
    expect(screen.getAllByText('warn').length).toBeGreaterThan(1);
  });

  it('默认只查未处理（resolved=false 带在请求里）', async () => {
    const fetchMock = renderPanel();
    await screen.findByText('CROSS_SRC_DIFF');
    expect(fetchMock.mock.calls[0][0]).toContain('resolved=false');
  });

  it('切换 severity 过滤触发新请求', async () => {
    const fetchMock = renderPanel();
    await screen.findByText('CROSS_SRC_DIFF');
    fireEvent.change(screen.getByLabelText('severity 过滤'), { target: { value: 'fatal' } });
    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('severity=fatal'));
      expect(calls.length).toBeGreaterThan(0);
    });
  });

  it('批量勾选 → resolve POST {ids} 并刷新', async () => {
    const fetchMock = renderPanel(async (url, init) => {
      if (url.includes('/data/issues/resolve') && init?.method === 'POST') {
        return new Response(JSON.stringify({ resolved: 1 }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/data/issues')) {
        return new Response(JSON.stringify(issues), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('not found', { status: 404 });
    });
    await screen.findByText('CROSS_SRC_DIFF');
    fireEvent.click(screen.getByLabelText('勾选 i1'));
    fireEvent.click(screen.getByText('标记已处理（1）'));
    await waitFor(() => {
      expect(screen.getByText(/✓ 已标记 1 条/)).toBeInTheDocument();
    });
    const postCalls = fetchMock.mock.calls.filter(
      ([u, i]) => String(u).includes('/data/issues/resolve') && (i as RequestInit).method === 'POST',
    );
    expect(postCalls).toHaveLength(1);
    expect(JSON.parse((postCalls[0][1] as RequestInit).body as string)).toEqual({ ids: ['i1'] });
  });

  it('加载失败：显式错误态', async () => {
    renderPanel(() =>
      new Response(JSON.stringify({ detail: 'boom' }), {
        status: 500, headers: { 'Content-Type': 'application/json' },
      }),
    );
    expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
  });
});
