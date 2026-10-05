import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

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

  // 运行中：此前右栏始终显示「点编译运行开始第一次回测」空态，用户点了运行后
  // 这一栏毫无变化，无法判断跑没跑起来，也没有取消入口。
  it('运行中显示进度态与「取消运行」，且不再显示空态引导', () => {
    mockUseSWR.mockReturnValue({ data: undefined });
    const onCancel = vi.fn();
    render(<ResultPane runId={null} runningJobId="j1" onCancel={onCancel} />);

    expect(screen.getByText('回测运行中…')).toBeInTheDocument();
    expect(screen.queryByText('点「编译运行 ▶」开始第一次回测')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消运行' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('运行中未传 onCancel 时不渲染取消按钮（避免死按钮）', () => {
    mockUseSWR.mockReturnValue({ data: undefined });
    render(<ResultPane runId={null} runningJobId="j1" />);
    expect(screen.queryByRole('button', { name: '取消运行' })).not.toBeInTheDocument();
  });

  it('运行中优先于已有结果展示（不被上一次的 runId 盖住）', () => {
    mockUseSWR.mockReturnValue({ data: DETAIL, isLoading: false });
    render(<ResultPane runId="r1" runningJobId="j2" onCancel={vi.fn()} />);
    expect(screen.getByText('回测运行中…')).toBeInTheDocument();
    expect(screen.queryByText('收益')).not.toBeInTheDocument();
  });

  it('运行结束后（runningJobId 清空）回到既有结果展示', () => {
    mockUseSWR.mockReturnValue({ data: DETAIL, isLoading: false });
    render(<ResultPane runId="r1" runningJobId={null} />);
    expect(screen.queryByText('回测运行中…')).not.toBeInTheDocument();
    expect(screen.getByText('收益')).toBeInTheDocument();
  });
});
