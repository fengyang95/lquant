import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import GapsPanel from '../GapsPanel';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

const gapResp = {
  window: { start: '2026-08-17', end: '2026-09-16' },
  datasets: [
    {
      dataset: 'daily',
      label: '日线湖',
      expected_days: 5,
      actual_days: 4,
      missing: ['2026-09-11'],
      sparse_symbols: {},
      sparse_total: 3,
    },
    {
      dataset: 'daily_basic',
      label: '估值指标湖',
      expected_days: 5,
      actual_days: 0,
      missing: [],
      sparse_symbols: {},
      sparse_total: 0,
    },
  ],
};

function renderPanel() {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (url.includes('/data/gaps?days=30')) {
      return new Response(JSON.stringify(gapResp), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function ui() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <GapsPanel />
    </SWRConfig>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('GapsPanel', () => {
  it('shows per-dataset days and missing dates', async () => {
    renderPanel();
    ui();
    expect(await screen.findByText('日线湖')).toBeInTheDocument();
    expect(screen.getByText(/4\/5 天/)).toBeInTheDocument();
    expect(screen.getByText(/缺 1 天/)).toBeInTheDocument();
    expect(screen.getByText(/2026-09-11/)).toBeInTheDocument();
    expect(screen.getByText(/3 只标的/)).toBeInTheDocument();
  });

  it('no repair button when no missing dates', async () => {
    renderPanel();
    ui();
    expect(await screen.findByText('估值指标湖')).toBeInTheDocument();
    expect(screen.getByText(/0\/5 天/)).toBeInTheDocument();
    // daily 有缺口 → 只有 1 个补采按钮；daily_basic 无缺口无按钮
    expect(screen.getAllByText('补采')).toHaveLength(1);
  });

  it('repair creates daily_update task', async () => {
    const fetchMock = renderPanel();
    const origImpl = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes('/data/gaps/repair')) {
        return new Response(JSON.stringify({ created: true, task_id: 'x1', reason: null }), {
          status: 202,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return origImpl(url);
    });
    ui();
    fireEvent.click(await screen.findByText('补采'));
    await waitFor(() => expect(screen.getByText(/已创建补采任务/)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/data/gaps/repair'),
      expect.objectContaining({ method: 'POST' }),
    );
  });
});
