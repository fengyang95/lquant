// BacktestPanel 测试 —— SWR 取数/轮询节奏、sweep 发起校验与请求体、取消按钮注入
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const useSWRMock = vi.fn();

vi.mock('swr', () => ({
  __esModule: true,
  default: (...args: unknown[]) => useSWRMock(...args),
}));

const postMock = vi.fn();

vi.mock('@/lib/api', () => ({
  fetcher: vi.fn(),
  post: (...args: unknown[]) => postMock(...args),
}));

import BacktestPanel from '../BacktestPanel';
import type { TaskItem } from '../../types';

const active: TaskItem = {
  id: 'aaaaaaaa-0000-0000-0000-000000000000',
  kind: 'backtest',
  name: 'sweep pct_change_20',
  status: 'running',
  state: 'running',
  created_at: '2024-06-01T10:30:45',
  params: { formula: 'pct_change_20', window: 20 },
  error: null,
};

const done: TaskItem = {
  ...active,
  id: 'bbbbbbbb-0000-0000-0000-000000000000',
  state: 'finished',
  status: 'ok',
};

function setup(tasks: TaskItem[] | undefined) {
  useSWRMock.mockReturnValue({ data: tasks, isLoading: false, mutate: vi.fn() });
  render(<BacktestPanel />);
}

describe('BacktestPanel', () => {
  beforeEach(() => {
    useSWRMock.mockReset();
    postMock.mockReset();
    postMock.mockResolvedValue({});
  });

  it('经 useSWR(\'/tasks?kind=backtest\', fetcher) 取数', () => {
    setup([active, done]);
    expect(useSWRMock).toHaveBeenCalledWith('/tasks?kind=backtest', expect.any(Function), expect.objectContaining({ refreshInterval: expect.any(Function) }));
  });

  it('运行中任务渲染「取消」按钮，已完成任务不渲染', () => {
    setup([active, done]);
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument();
    // 详情列透出 formula
    expect(screen.getAllByText('pct_change_20').length).toBeGreaterThan(0);
  });

  it('点「取消」→ POST /tasks/backtest/{id}/cancel，成功透出反馈', async () => {
    postMock.mockResolvedValue({});
    setup([active]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/tasks/backtest/aaaaaaaa-0000-0000-0000-000000000000/cancel', {});
    });
    expect(screen.getByText(/已请求取消/)).toBeInTheDocument();
  });

  it('取消失败 → 透出 ✗ 错误反馈', async () => {
    postMock.mockRejectedValue(new Error('502 bad gateway'));
    setup([active]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      expect(screen.getByText(/✗ 502 bad gateway/)).toBeInTheDocument();
    });
  });

  it('未填参数名/参数值 → 校验报错且不发请求', async () => {
    setup([]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '发起 Sweep' }));
    expect(screen.getByText('✗ 请填写公式、参数名和至少一个参数值')).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it('填全表单 → POST /backtests/sweep，逗号/中文逗号拆分参数值', async () => {
    setup([]);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/参数名/), 'window');
    await user.type(screen.getByLabelText(/参数值/), '5, 10，20');
    await user.click(screen.getByRole('button', { name: '发起 Sweep' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/backtests/sweep', {
        formula: 'pct_change_20',
        param: 'window',
        values: ['5', '10', '20'],
        rebalance: 'monthly',
      });
    });
    expect(screen.getByText(/✓ sweep 已发起：window ∈ \{5, 10, 20\}/)).toBeInTheDocument();
  });
});
