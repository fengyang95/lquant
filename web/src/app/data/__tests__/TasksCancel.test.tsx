import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import TasksPanel from '../TasksPanel';
import type { DataTask } from '../types';

// jsdom 无 canvas，echarts 初始化即崩（clearRect of null）—— 桩掉渲染层
vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

const runningTask: DataTask = {
  task_id: 't-12345678',
  kind: 'full_backfill',
  params: { start: '2016-01-01', end: '2026-09-12' },
  status: 'running',
  phase: 'fetch',
  total_symbols: 100,
  done_symbols: 40,
  failed_symbols: [],
  failed_detail: [],
  rows_written: 4000,
  started_at: '2026-09-12T10:00:00',
  finished_at: null,
  message: null,
};

function renderPanel(
  handler?: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (handler) return handler(url, init);
    if (url.includes('/data/tasks') && !url.includes('/cancel')) {
      return new Response(JSON.stringify([runningTask]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <TasksPanel />
    </SWRConfig>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('TasksPanel 任务取消', () => {
  it('running 任务显示「取消」按钮，点击 POST /tasks/data/{id}/cancel', async () => {
    const fetchMock = renderPanel(async (url, init) => {
      if (url.includes('/tasks/data/') && url.includes('/cancel')) {
        return new Response(JSON.stringify({ canceled: true }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify([runningTask]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    });
    expect(await screen.findByText(/40\/100/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /取消任务 t-12345/ }));
    await waitFor(() => {
      expect(screen.getByText(/✓ 已请求取消/)).toBeInTheDocument();
    });
    const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/cancel'));
    expect(calls).toHaveLength(1);
    expect(String(calls[0][0])).toContain('/api/tasks/data/t-12345678/cancel');
  });

  it('409 已结束：提示而非报错', async () => {
    renderPanel(async (url) => {
      if (url.includes('/tasks/data/') && url.includes('/cancel')) {
        return new Response(JSON.stringify({ detail: '任务已结束，无法取消' }), {
          status: 409, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify([runningTask]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    });
    await screen.findByText(/40\/100/);
    fireEvent.click(screen.getByRole('button', { name: /取消任务/ }));
    expect(await screen.findByText(/任务已结束，无需取消/)).toBeInTheDocument();
  });

  it('failed 任务无取消按钮', async () => {
    const failed: DataTask = { ...runningTask, status: 'failed' };
    renderPanel(() =>
      new Response(JSON.stringify([failed]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }),
    );
    await screen.findByText(/40\/100/);
    expect(screen.queryByRole('button', { name: /取消任务/ })).not.toBeInTheDocument();
  });
});
