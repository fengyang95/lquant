import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import CheckpointPanel from '../CheckpointPanel';
import type { DataTask } from '../types';

type Cp = { name: string; done: number; updated_at: string | null; meta: Record<string, unknown> };

function renderPanel(cps: Cp[]) {
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.includes('/data/checkpoints') && init?.method === 'DELETE') {
      const name = decodeURIComponent(url.split('/checkpoints/')[1]);
      return new Response(
        JSON.stringify({ name, archived_to: `${name}.done-20260912-224500` }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }
    if (url.includes('/data/checkpoints')) {
      return new Response(JSON.stringify(cps), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <CheckpointPanel />
    </SWRConfig>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('CheckpointPanel 断点续传管理', () => {
  const cps: Cp[] = [
    { name: 'daily', done: 656, updated_at: '2026-09-12T14:00:00', meta: { start: '2024-01-01', end: '2025-12-31' } },
    { name: 'daily-2026.done', done: 6877, updated_at: '2026-09-01T01:00:00', meta: {} },
  ];

  it('空态：引导文案', () => {
    renderPanel([]);
    expect(screen.getByText(/暂无断点文件/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('列表：断点名/已完成数/元信息可见', async () => {
    renderPanel(cps);
    expect(await screen.findByText('daily')).toBeInTheDocument();
    expect(screen.getByText('daily-2026.done')).toBeInTheDocument();
    expect(screen.getByText('656')).toBeInTheDocument();
    expect(screen.getByText('6,877')).toBeInTheDocument();
    expect(screen.getByText(/start=2024-01-01/)).toBeInTheDocument();
  });

  it('归档：确认后 DELETE 并刷新列表', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = renderPanel(cps);
    fireEvent.click((await screen.findAllByRole('button', { name: '归档' }))[0]);
    await waitFor(() => {
      expect(screen.getByText(/✓ 已归档 daily →/)).toBeInTheDocument();
    });
    expect(confirmSpy).toHaveBeenCalledOnce();
    const delCalls = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === 'DELETE');
    expect(delCalls).toHaveLength(1);
    expect(delCalls[0][0]).toContain('/data/checkpoints/daily');
  });

  it('取消确认：不发 DELETE', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const fetchMock = renderPanel(cps);
    fireEvent.click((await screen.findAllByRole('button', { name: '归档' }))[0]);
    await waitFor(() => {
      const delCalls = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === 'DELETE');
      expect(delCalls).toHaveLength(0);
    });
  });

  it('归档失败：错误消息透出', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes('/data/checkpoints') && init?.method === 'DELETE') {
        return new Response(JSON.stringify({ detail: '断点不存在: daily' }), {
          status: 404, headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/data/checkpoints')) {
        return new Response(JSON.stringify(cps), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('not found', { status: 404 });
    }));
    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <CheckpointPanel />
      </SWRConfig>,
    );
    fireEvent.click((await screen.findAllByRole('button', { name: '归档' }))[0]);
    expect(await screen.findByText(/✗/)).toBeInTheDocument();
  });

  it('加载失败：显式错误态，不误显为「暂无断点」', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/data/checkpoints')) {
        return new Response(JSON.stringify({ detail: '内部错误' }), {
          status: 500, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('not found', { status: 404 });
    }));
    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <CheckpointPanel />
      </SWRConfig>,
    );
    expect(await screen.findByText(/加载断点列表失败/)).toBeInTheDocument();
    expect(screen.queryByText(/暂无断点文件/)).not.toBeInTheDocument();
  });
});
