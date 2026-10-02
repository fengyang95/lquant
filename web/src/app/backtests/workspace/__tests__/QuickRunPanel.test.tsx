// QuickRunPanel 测试 —— 表单迁移自旧回测页；点击「运行回测」→ post + 全局 mutate('/backtests')
// 新增退出策略选择器：未选时不发 exit_strategy 字段（旧契约不破）
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const postMock = vi.fn();
const mutateMock = vi.fn();
const exitStrategies = [
  { name: 'tiered', label: '分级移动止盈' },
  { name: 'simple', label: '固定止损止盈' },
];

vi.mock('swr', () => ({
  mutate: (...args: unknown[]) => mutateMock(...args),
  default: () => ({ data: { strategies: exitStrategies } }),
}));

vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  fetcher: vi.fn(),
  post: (...args: unknown[]) => postMock(...args),
}));

import QuickRunPanel from '../QuickRunPanel';

describe('QuickRunPanel', () => {
  beforeEach(() => {
    postMock.mockReset();
    mutateMock.mockReset();
    postMock.mockResolvedValue({});
  });

  it('渲染表单默认值并渲染「运行回测」按钮', () => {
    render(<QuickRunPanel />);
    expect(screen.getByText('因子公式')).toBeInTheDocument();
    expect(screen.getByDisplayValue('pct_change_20')).toBeInTheDocument();
    expect(screen.getByDisplayValue('5')).toBeInTheDocument();
    expect(screen.getByDisplayValue('monthly')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行回测' })).toBeInTheDocument();
  });

  it('点击「运行回测」→ post 以默认参数被调用，并触发全局 mutate(\'/backtests\')', async () => {
    render(<QuickRunPanel />);
    await userEvent.click(screen.getByRole('button', { name: '运行回测' }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/backtests/run', {
        top_n: 5,
        rebalance: 'monthly',
        formula: 'pct_change_20',
      });
    });
    expect(mutateMock).toHaveBeenCalledWith('/backtests');
  });

  it('post 失败时显示错误信息且不 mutate', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    render(<QuickRunPanel />);
    await userEvent.click(screen.getByRole('button', { name: '运行回测' }));

    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument();
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it('退出策略选择器列出后端注册的策略，默认「不启用」', () => {
    render(<QuickRunPanel />);
    const sel = screen.getByLabelText('退出策略') as HTMLSelectElement;
    expect(sel.value).toBe('');
    expect(screen.getByRole('option', { name: '不启用（纯因子轮动）' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '分级移动止盈' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '固定止损止盈' })).toBeInTheDocument();
  });

  it('选中退出策略后随请求体一起提交', async () => {
    render(<QuickRunPanel />);
    await userEvent.selectOptions(screen.getByLabelText('退出策略'), 'tiered');
    await userEvent.click(screen.getByRole('button', { name: '运行回测' }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/backtests/run', {
        top_n: 5,
        rebalance: 'monthly',
        formula: 'pct_change_20',
        exit_strategy: 'tiered',
      });
    });
  });
});
