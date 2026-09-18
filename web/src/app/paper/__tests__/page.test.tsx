import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PaperPage from '../page';
import { stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

const replayResp = {
  summary: {
    final_nav: 1088000,
    total_return: 0.088,
    n_orders: 12,
    n_filled: 11,
    n_rejected: 1,
    max_drawdown: -0.031,
    rejections: [],
  },
  positions: [
    { symbol: '600519.SH', qty: 100, available: 100, avg_cost: 1400, last_price: 1450, pnl_pct: 0.0357 },
  ],
  nav: [
    { trade_date: '2026-09-15', nav: 1.0 },
    { trade_date: '2026-09-16', nav: 1.012 },
  ],
  alerts: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('PaperPage', () => {
  it('离线回放成功：渲染回放指标与持仓表', async () => {
    const fetchMock = stubPageFetch({ '/paper/replay': replayResp });
    render(<PaperPage />);
    fireEvent.click(screen.getByRole('button', { name: '离线回放' }));

    await waitFor(() => expect(screen.getByText('回放结果')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/paper/replay',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(screen.getByText('600519.SH')).toBeInTheDocument();
    expect(screen.getByText('1,088,000')).toBeInTheDocument();
  });

  it('回放失败：ErrorNote 显示错误信息', async () => {
    stubPageFetch({ '/paper/replay': { detail: '撮合失败' } }, { '/paper/replay': 500 });
    render(<PaperPage />);
    fireEvent.click(screen.getByRole('button', { name: '离线回放' }));

    await waitFor(() => expect(screen.getByText(/撮合失败/)).toBeInTheDocument());
  });
});
