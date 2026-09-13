import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import SyncJobsPanel from '../SyncJobsPanel';
import type { SyncJob, SyncRunRecord } from '../types';

const jobs: SyncJob[] = [
  {
    sync_id: 'daily_close',
    name: '日终采集',
    kind: 'collect',
    schedule_time: '17:30',
    weekdays: '1,2,3,4,5',
    params: {},
    enabled: true,
  },
];

const history: SyncRunRecord[] = [
  {
    sync_id: 'daily_close',
    kind: 'collect',
    status: 'ok',
    started_at: '2026-09-12T17:30:00',
    finished_at: '2026-09-12T17:33:00',
    rows: 5400,
    error: null,
  },
];

function renderPanel() {
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.includes('/sync/jobs/daily_close/toggle')) {
      return new Response(JSON.stringify({ sync_id: 'daily_close', enabled: false }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/sync/run') && init?.method === 'POST') {
      return new Response(JSON.stringify({ accepted: true }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/sync/jobs')) {
      return new Response(JSON.stringify(jobs), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/sync/history')) {
      return new Response(JSON.stringify(history), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <SyncJobsPanel />
    </SWRConfig>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('SyncJobsPanel 同步作业', () => {
  it('作业列表与运行历史渲染', async () => {
    renderPanel();
    expect(await screen.findByText('日终采集')).toBeInTheDocument();
    expect(screen.getByText('17:30（周1,2,3,4,5）')).toBeInTheDocument();
    expect(screen.getByText('ok')).toBeInTheDocument();
    expect(screen.getByText('5,400')).toBeInTheDocument();
  });

  it('启停开关：POST toggle', async () => {
    const fetchMock = renderPanel();
    await screen.findByText('日终采集');
    fireEvent.click(screen.getByRole('button', { name: '停用' }));
    await waitFor(() => {
      expect(screen.getByText(/✓ 日终采集 已停用/)).toBeInTheDocument();
    });
    const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/sync/jobs/daily_close/toggle'));
    expect(calls).toHaveLength(1);
    expect(JSON.parse((calls[0][1] as RequestInit).body as string)).toEqual({ enabled: false });
  });

  it('立即执行：POST /sync/run {sync_id}', async () => {
    const fetchMock = renderPanel();
    await screen.findByText('日终采集');
    fireEvent.click(screen.getByRole('button', { name: '立即执行' }));
    await waitFor(() => {
      expect(screen.getByText(/✓ 日终采集 已触发/)).toBeInTheDocument();
    });
    const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/sync/run'));
    expect(calls).toHaveLength(1);
    expect(JSON.parse((calls[0][1] as RequestInit).body as string)).toEqual({ sync_id: 'daily_close' });
  });
});
