// HistoryPanel 测试 —— 记录表渲染 / 「载入」按钮回调 / 勾选对比按钮
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

const useSWRMock = vi.fn();

vi.mock('swr', () => ({
  __esModule: true,
  default: (...args: unknown[]) => useSWRMock(...args),
}));

vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('@/components/Chart', () => ({
  __esModule: true,
  default: () => <div data-testid="chart-stub" />,
}));

import HistoryPanel from '../HistoryPanel';
import type { RunRow } from '../HistoryPanel';

const rows: RunRow[] = [
  {
    run_id: 'run-a',
    strategy: 'factor_rotation',
    params: { factor: 'pct_change_20', top_n: 5, rebalance: 'monthly' },
    start_date: '2024-01-01',
    end_date: '2024-12-31',
    status: 'done',
    metrics: { total_return: 0.12, annual_return: 0.12, sharpe: 1.2, max_drawdown: 0.08 },
    created_at: '2024-06-01T10:00:00',
  },
  {
    run_id: 'run-b',
    strategy: 'factor_rotation',
    params: { factor: 'pct_change_5', top_n: 3, rebalance: 'weekly' },
    start_date: '2024-01-01',
    end_date: '2024-12-31',
    status: 'done',
    metrics: { total_return: -0.03, annual_return: -0.03, sharpe: -0.4, max_drawdown: 0.15 },
    created_at: '2024-06-02T10:00:00',
  },
];

function setup() {
  const onLoadRun = vi.fn();
  render(<HistoryPanel onLoadRun={onLoadRun} />);
  return { onLoadRun };
}

describe('HistoryPanel', () => {
  beforeEach(() => {
    useSWRMock.mockReset();
    useSWRMock.mockReturnValue({ data: rows, mutate: vi.fn() });
  });

  it('经 useSWR(\'/backtests\', get, { refreshInterval: 5000 }) 取数并渲染记录表', () => {
    setup();
    expect(useSWRMock).toHaveBeenCalledWith('/backtests', expect.any(Function), {
      refreshInterval: 5000,
    });
    expect(screen.getByText('run-a')).toBeInTheDocument();
    expect(screen.getByText('run-b')).toBeInTheDocument();
    expect(screen.getByText('pct_change_20 · Top5 · monthly')).toBeInTheDocument();
  });

  it('点「载入」→ onLoadRun 以该行对象被调用', () => {
    const { onLoadRun } = setup();
    fireEvent.click(screen.getAllByRole('button', { name: '载入' })[0]);
    expect(onLoadRun).toHaveBeenCalledWith(rows[0]);
  });

  it('勾选两行 → 「对比选中 2 项」按钮出现；取消勾选到不足 2 项时消失', () => {
    setup();
    expect(screen.queryByRole('button', { name: /对比选中/ })).not.toBeInTheDocument();

    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    expect(screen.getByRole('button', { name: '对比选中 2 项' })).toBeInTheDocument();

    fireEvent.click(boxes[0]);
    expect(screen.queryByRole('button', { name: /对比选中/ })).not.toBeInTheDocument();
  });

  it('空列表显示空态文案', () => {
    useSWRMock.mockReturnValue({ data: [], mutate: vi.fn() });
    setup();
    expect(screen.getByText('还没有回测 —— 用上方表单跑一个')).toBeInTheDocument();
  });
});
