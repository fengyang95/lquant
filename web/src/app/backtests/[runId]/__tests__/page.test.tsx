import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import BacktestDetailPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

const { useParams } = vi.hoisted(() => ({
  useParams: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useParams,
}));

const detail = {
  strategy: 'demo',
  status: 'done',
  params: { start: '2025-01-01', end: '2025-12-31', initial_cash: 1000000, formula: 'demo 因子' },
  metrics: {
    total_return: 0.123, annual_return: 0.13, sharpe: 1.2, sortino: 1.5,
    win_rate: 0.55, payoff_ratio: 1.4, max_drawdown: -0.08, annual_vol: 0.11,
    total_fee: 1200, n_trades: 30, n_rejected: 0, initial_cash: 1000000,
  },
  risk_vs_benchmark: {
    benchmark: '沪深300', excess_return: 0.05, alpha_annual: 0.04, beta: 0.8,
    information_ratio: 0.6, tracking_error: 0.09,
  },
  nav: [
    { date: '2025-01-02', nav: 1.0, drawdown: 0 },
    { date: '2025-01-03', nav: 1.01, drawdown: 0 },
  ],
  benchmark_label: '沪深300',
  benchmark: [
    { date: '2025-01-02', nav: 1.0 },
    { date: '2025-01-03', nav: 1.005 },
  ],
  monthly: [
    { year: 2025, month: 1, ret: 0.02 },
  ],
  return_hist: [{ lo: -0.01, hi: 0.0, count: 5 }],
  rolling: [{ date: '2025-01-03', vol: 0.1, sharpe: 1.1 }],
  records: {},
  custom_analysis: [],
  logs: ['step1 ok'],
};

afterEach(() => {
  vi.unstubAllGlobals();
  useParams.mockReset();
  useParams.mockReturnValue({ runId: 'r1' });
});

describe('BacktestDetailPage', () => {
  beforeEach(() => {
    useParams.mockReturnValue({ runId: 'r1' });
  });

  it('正常数据：渲染指标条与 Tab 壳', async () => {
    stubPageFetch({
      '/backtests/r1/attribution': { risk: {}, stock_contribution: null, brinson: null },
      '/backtests/r1/code': { code: 'x' },
      '/backtests/r1': detail,
    });
    renderPage(<BacktestDetailPage />);

    await waitFor(() => expect(screen.getByText('策略收益')).toBeInTheDocument());
    expect(screen.getByText('收益概述')).toBeInTheDocument();
    expect(screen.getByText('归因分析')).toBeInTheDocument();
    expect(screen.getByText('单笔进出')).toBeInTheDocument();
  });

  it('fetch 失败：Loading 空态，不崩溃', async () => {
    stubPageFetch({});
    renderPage(<BacktestDetailPage />);

    await waitFor(() =>
      expect(screen.getByText(/加载中…/)).toBeInTheDocument(),
    );
  });
});

