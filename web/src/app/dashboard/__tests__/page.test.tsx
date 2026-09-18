import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import DashboardPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const breadth = {
  latest: {
    trade_date: '2026-09-16', n: 5000, up: 3200, down: 1600, flat: 200,
    limit_up: 60, limit_down: 10, med_chg: 0.012, total_amount: 1.2e10, up_ratio: 0.64,
  },
  history: [
    {
      trade_date: '2026-09-15', n: 5000, up: 2800, down: 2000, flat: 200,
      limit_up: 45, limit_down: 12, med_chg: 0.004, total_amount: 1.1e10, up_ratio: 0.56,
    },
    {
      trade_date: '2026-09-16', n: 5000, up: 3200, down: 1600, flat: 200,
      limit_up: 60, limit_down: 10, med_chg: 0.012, total_amount: 1.2e10, up_ratio: 0.64,
    },
  ],
};

const indexRows = [
  {
    symbol: '000001.SH', name: '上证指数', close: 3245.6, chg: 0.008, trade_date: '2026-09-16',
    dates: ['2026-09-15', '2026-09-16'], closes: [3220.1, 3245.6],
  },
];

const batch = {
  symbols: ['600519.SH'],
  latest: [
    { symbol: '600519.SH', name: '贵州茅台', trade_date: '2026-09-16', close: 1450, chg: 0.012, amount_yi: 32.5 },
  ],
  summary: {
    n: 1, up: 1, down: 0, avg_chg: 0.012,
    best: { symbol: '600519.SH', name: '贵州茅台', trade_date: '2026-09-16', close: 1450, chg: 0.012, amount_yi: 32.5 },
    worst: { symbol: '600519.SH', name: '贵州茅台', trade_date: '2026-09-16', close: 1450, temp: 0, chg: 0.012, amount_yi: 32.5 },
  },
  dates: ['2026-09-15', '2026-09-16'],
  series: { '600519.SH': [1.0, 1.012] },
  equal_weight_nav: [1.0, 1.012],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DashboardPage', () => {
  it('正常数据：渲染指数条/宽度指标/批量对比表', async () => {
    stubPageFetch({
      '/market/overview': {
        sentiment: {
          score: 62, limit_up: 60, limit_down: 10, broken_rate: 0.2,
          max_consecutive: 5, trade_date: '2026-09-16',
        },
        sentiment_history: [
          { trade_date: '2026-09-16', sentiment_score: 62, limit_up_count: 60, broken_rate: 0.2 },
        ],
        northbound: [
          { trade_date: '2026-09-16', sh_net_inflow: 5.2e8, sz_net_inflow: 3.1e8, total_net_inflow: 8.3e8 },
        ],
      },
      '/market/breadth': breadth,
      '/market/index': indexRows,
      '/market/batch': batch,
    });
    renderPage(<DashboardPage />);

    await waitFor(() => expect(screen.getByText('上证指数')).toBeInTheDocument());
    expect(screen.getByText('大盘')).toBeInTheDocument();
    expect(screen.getAllByText('贵州茅台').length).toBeGreaterThan(0);
    expect(screen.getByText('温度计')).toBeInTheDocument();
    expect(screen.getAllByText('亢奋').length).toBeGreaterThan(0);
    expect(screen.getByText('北向资金')).toBeInTheDocument();
  });

  it('fetch 失败：壳仍在，降级空态，不崩溃', async () => {
    stubPageFetch({});
    renderPage(<DashboardPage />);

    await waitFor(() => expect(screen.getByText('大盘')).toBeInTheDocument());
    expect(screen.getByText('批量对比')).toBeInTheDocument();
    // 空态：情绪历史空提示
    expect(screen.getByText(/暂无 —— 跑/)).toBeInTheDocument();
  });
});
