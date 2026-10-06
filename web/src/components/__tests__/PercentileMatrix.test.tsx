// 行业分位表 —— 分数页面必须能回答「这个档位是按什么分布划的」
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const getMock = vi.fn();
const swrState: { data: unknown; isLoading: boolean; error: unknown } = {
  data: undefined, isLoading: false, error: undefined,
};

vi.mock('@/lib/api', () => ({
  get: (...a: unknown[]) => getMock(...a),
  fetcher: (...a: unknown[]) => getMock(...a),
}));

vi.mock('swr', () => ({ default: () => swrState }));

import PercentileMatrix from '../PercentileMatrix';

const ROWS = {
  asof: '2026-04-01',
  available: true,
  rows: [
    { industry: '白酒', item: 'indicator.roe', label: '净资产收益率',
      module: 'profitability', p25: 10, p50: 15, p75: 20, n: 20 },
    { industry: '白酒', item: 'indicator.debt_to_assets', label: '资产负债率',
      module: 'solvency', p25: 30, p50: 45, p75: 60, n: 20 },
    { industry: '银行', item: 'indicator.roe', label: '净资产收益率',
      module: 'profitability', p25: 8, p50: 12, p75: 16, n: 42 },
  ],
};

describe('PercentileMatrix', () => {
  beforeEach(() => {
    getMock.mockReset();
    swrState.data = ROWS;
    swrState.isLoading = false;
    swrState.error = undefined;
  });

  it('加载中显示 Loading', () => {
    swrState.isLoading = true;
    render(<PercentileMatrix />);
    expect(screen.getByText('加载中…')).toBeInTheDocument();
  });

  it('请求失败显示错误', () => {
    swrState.error = new Error('boom');
    render(<PercentileMatrix />);
    expect(screen.getByText(/分位加载失败/)).toBeInTheDocument();
  });

  it('不可用时显示后端 hint 而不是空表', () => {
    swrState.data = { asof: '2026-04-01', available: false, hint: '样本不足', rows: [] };
    render(<PercentileMatrix />);
    expect(screen.getByText('样本不足')).toBeInTheDocument();
  });

  it('渲染指标选择器与行业分位表', () => {
    render(<PercentileMatrix />);
    expect(screen.getByLabelText('分位指标')).toBeInTheDocument();
    // 默认选中第一个指标 → 只显示该指标的行业行
    expect(screen.getByText('白酒')).toBeInTheDocument();
    expect(screen.getByText('银行')).toBeInTheDocument();
    expect(screen.getByText('15.00')).toBeInTheDocument();   // 白酒 P50
    expect(screen.getByText('42')).toBeInTheDocument();      // 银行样本数
  });

  it('切换指标后表格内容随之变化', async () => {
    render(<PercentileMatrix />);
    fireEvent.change(screen.getByLabelText('分位指标'), {
      target: { value: 'indicator.debt_to_assets' },
    });
    await waitFor(() => expect(screen.getByText('45.00')).toBeInTheDocument());
    // 换指标后银行没有该指标的行
    expect(screen.queryByText('42')).not.toBeInTheDocument();
  });

  it('把观察日与样本阈值写进口径说明', () => {
    render(<PercentileMatrix asof="2026-04-01" minSamples={8} />);
    expect(screen.getByText(/观察日 2026-04-01/)).toBeInTheDocument();
    expect(screen.getByText(/样本不足 8 的行业不出分位/)).toBeInTheDocument();
    expect(screen.getByText(/正向指标 ≥P75 得满分/)).toBeInTheDocument();
  });
});
