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
          {
            trade_date: '2026-09-16', sh_deal_amt: 3.2e11, sz_deal_amt: 2.8e11,
            total_deal_amt: 6.0e11, deal_num: 6824276,
            sh_net_inflow: null, sz_net_inflow: null, total_net_inflow: null,
            net_published: false,
          },
        ],
      },
      '/market/northbound': {
        flow: [],
        top10: [
          {
            board: '沪股通', symbol: '600519.SH', name: '贵州茅台', rank_no: 8,
            close: 1255.79, change_pct: -0.22, deal_amt: 1.33e9, mutual_ratio: 36.72,
          },
        ],
        net_last_date: '2024-08-16',
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
    // 北向：净买额停发，面板只展示成交额；不把 null 画成 0
    expect(screen.getByText('北向成交额')).toBeInTheDocument();
    expect(screen.getByText('北向前十大成交活跃证券')).toBeInTheDocument();
    expect(screen.getByText('36.7%')).toBeInTheDocument();
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
