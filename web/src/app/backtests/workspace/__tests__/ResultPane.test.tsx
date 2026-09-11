import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

const mockUseSWR = vi.fn();

vi.mock('swr', () => ({
  default: (key: unknown, fetcher: unknown) =>
    mockUseSWR(key, fetcher),
}));

vi.mock('@/components/Chart', () => ({
  default: () => <div data-testid="chart-stub" />,
}));

import ResultPane from '../ResultPane';

const DETAIL = {
  run_id: 'r1',
  strategy: 'demo',
  params: {},
  status: 'done',
  metrics: {
    total_return: 0.1523,
    annual_return: 0.3,
    sharpe: 1.82,
    max_drawdown: -0.08,
    win_rate: 0.61,
    total_fee: 1234,
  },
  nav: [
    { date: '2024-01-02', nav: 1.01, drawdown: null },
    { date: '2024-01-03', nav: 1.05, drawdown: -0.01 },
  ],
  benchmark: [{ date: '2024-01-02', nav: 1.0 }, { date: '2024-01-03', nav: 1.02 }],
  benchmark_label: '沪深300',
  logs: ['line1', 'line2'],
};

describe('ResultPane', () => {
  beforeEach(() => {
    mockUseSWR.mockReset();
  });

  it('runId 为 null 时渲染 Empty 空态且不发起请求', () => {
    mockUseSWR.mockReturnValue({ data: undefined });
    render(<ResultPane runId={null} />);
    expect(mockUseSWR).toHaveBeenCalledWith(null, expect.any(Function));
    expect(screen.getByText('点「编译运行 ▶」开始第一次回测')).toBeInTheDocument();
  });

  it('有 data 时渲染六项指标卡、净值图与完整详情链接', () => {
    mockUseSWR.mockReturnValue({ data: DETAIL, isLoading: false });
    render(<ResultPane runId="r1" />);

    // SWR key 与 fetcher 正确
    expect(mockUseSWR).toHaveBeenCalledWith('/backtests/r1', expect.any(Function));

    for (const label of ['收益', '年化', '夏普', '回撤', '胜率', '费用']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // 涨红跌绿
    expect(screen.getByText('15.23%')).toHaveClass('text-up');
    expect(screen.getByText('-8.00%')).toHaveClass('text-down');

    expect(screen.getByTestId('chart-stub')).toBeInTheDocument();

    const link = screen.getByRole('link', { name: /查看完整详情/ });
    expect(link).toHaveAttribute('href', '/backtests/r1');

    // 日志折叠
    expect(screen.getByText('运行日志（2 条）')).toBeInTheDocument();
  });

  it('data 未到时渲染 Loading', () => {
    mockUseSWR.mockReturnValue({ data: undefined, isLoading: true });
    render(<ResultPane runId="r2" />);
    expect(screen.getByText(/加载中/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /查看完整详情/ })).not.toBeInTheDocument();
  });
});
