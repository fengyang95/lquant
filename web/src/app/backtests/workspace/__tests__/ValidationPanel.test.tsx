// ValidationPanel 测试 —— 自检表格渲染 + 基准策略一键回测（post → run_id 链接）
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const postMock = vi.fn();

const swrMock = vi.hoisted(() => ({ data: undefined as undefined | Record<string, unknown> }));
vi.mock('swr', () => ({
  default: () => ({
    data: swrMock.data,
    isLoading: false,
    error: undefined,
    mutate: vi.fn(),
  }),
  mutate: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  post: (...args: unknown[]) => postMock(...args),
}));

import ValidationPanel from '../ValidationPanel';

const VALIDATION = {
  checks: [
    { name: '金标准净值（手算逐日对账，零费率）', passed: true, detail: '净值序列一致' },
    { name: '涨跌停拒单（开盘封板不可买）', passed: false, detail: '未拦截' },
  ],
  all_passed: false,
  benchmarks: {
    sma_cross: {
      label: '双均线金叉死叉（5/20）',
      symbols: ['600519.SH'],
      description: 'SMA5 上穿 SMA20 满仓，下穿清仓。',
      reference: 'backtrader 教程案例：累计 -34.68%',
    },
    momentum_rotation: {
      label: '宽基 ETF 动量单强轮动（22日）',
      symbols: ['510300.SH', '159915.SZ'],
      description: '满仓动量最高者；全体为负时空仓。',
      reference: '雪球：年化 18.72%',
    },
  },
};

describe('ValidationPanel', () => {
  beforeEach(() => {
    postMock.mockReset();
    postMock.mockResolvedValue({ run_id: 'abc123' });
    swrMock.data = VALIDATION as unknown as Record<string, unknown>;
  });

  it('渲染自检统计与逐项检查表（含失败项）', () => {
    render(<ValidationPanel />);
    expect(screen.getByText('引擎自检')).toBeInTheDocument();
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    expect(screen.getByText('存在失败')).toBeInTheDocument();
    expect(screen.getByText('✓ 通过')).toBeInTheDocument();
    expect(screen.getByText('✗ 失败')).toBeInTheDocument();
    expect(screen.getByText('未拦截')).toBeInTheDocument();
  });

  it('渲染基准策略卡片与公开出处', () => {
    render(<ValidationPanel />);
    expect(screen.getByText('双均线金叉死叉（5/20）')).toBeInTheDocument();
    expect(screen.getByText('雪球：年化 18.72%')).toBeInTheDocument();
  });

  it('点击「运行回测」→ post /backtests/run-benchmark 并展示结果链接', async () => {
    render(<ValidationPanel />);
    const buttons = screen.getAllByRole('button', { name: '运行回测' });
    await userEvent.click(buttons[0]);

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/backtests/run-benchmark', { key: 'sma_cross' });
    });
    await waitFor(() => {
      expect(screen.getByText(/已运行/)).toBeInTheDocument();
      expect(screen.getByText(/abc123/)).toBeInTheDocument();
    });
  });

  it('post 失败时显示错误信息', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    render(<ValidationPanel />);
    await userEvent.click(screen.getAllByRole('button', { name: '运行回测' })[0]);
    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument();
    });
  });
});
