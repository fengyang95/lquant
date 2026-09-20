import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SWRConfig } from 'swr';
import FreshnessHealthCard, { lagTone } from '../FreshnessHealthCard';

function mockFetch(responses: Record<string, unknown>) {
  return vi.fn().mockImplementation(async (url: string) => {
    for (const [key, value] of Object.entries(responses)) {
      if (url.includes(key)) {
        return new Response(JSON.stringify(value), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    return new Response('not found', { status: 404 });
  });
}

function renderCard(responses: Record<string, unknown>) {
  const f = mockFetch(responses);
  vi.stubGlobal('fetch', f);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <FreshnessHealthCard />
    </SWRConfig>,
  );
  return f;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('lagTone', () => {
  it('lag=0 绿 / 1 黄 / >1 红 / null 灰', () => {
    expect(lagTone(0)).toBe('down');
    expect(lagTone(1)).toBe('gold');
    expect(lagTone(3)).toBe('up');
    expect(lagTone(null)).toBe('faint');
    expect(lagTone(undefined)).toBe('faint');
    expect(lagTone(Number.NaN)).toBe('faint');
  });
});

describe('FreshnessHealthCard', () => {
  it('新鲜数据 → 绿点 + 最新日期 + 落后 0 交易日', async () => {
    renderCard({
      '/sync/freshness': {
        daily_lake: '2026-09-18',
        news: { latest: '2026-09-18 15:00:00', today_rows: 42 },
        financial_pit: { covered_end: '2026-09-18', symbols: 5000, marked: 5000 },
        lag_days: 0,
      },
      '/data/gaps': { datasets: [] },
    });
    expect((await screen.findAllByText('2026-09-18')).length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText(/状态：down/).length).toBe(3);
    expect(screen.getByText('落后 0 个交易日')).toBeTruthy();
  });

  it('lag>1 → 红点；缺口数据集红色高亮', async () => {
    renderCard({
      '/sync/freshness': { daily_lake: '2026-09-10', news: null, financial_pit: null, lag_days: 3 },
      '/data/gaps': {
        datasets: [
          { dataset: 'daily', label: '日线', expected_days: 20, actual_days: 17, missing: ['2026-09-11', '2026-09-12', '2026-09-15'], sparse_total: 0 },
        ],
      },
    });
    await screen.findByText('2026-09-10');
    expect(screen.getAllByLabelText(/状态：up/).length).toBeGreaterThan(0);
    expect(screen.getByText('日线 缺 3 日')).toBeTruthy();
  });

  it('freshness 请求失败 → 全部灰点（未知）', async () => {
    const f = vi.fn().mockResolvedValue(new Response('err', { status: 500 }));
    vi.stubGlobal('fetch', f);
    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <FreshnessHealthCard />
      </SWRConfig>,
    );
    await screen.findByText('日线湖');
    expect(screen.getAllByLabelText(/状态：faint/).length).toBe(3);
  });
});
