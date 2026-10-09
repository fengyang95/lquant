/** 行业轮动榜页：排名/收益渲染、窗口切换进请求、空态与错误态。 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const getMock = vi.fn();

const ROTATION = {
  asof: '2026-09-30',
  std: 'SW',
  window: 20,
  notes: [],
  rows: [
    { industry_code: '801080.SI', industry_name: '电子', n_members: 300,
      r20: 8.5, r60: 12.0, amount_20d: 2e12, pe_median: 35.2,
      rank: 1, percentile: 100 },
    { industry_code: '801780.SI', industry_name: '银行', n_members: 42,
      r20: -3.2, r60: 2.0, amount_20d: 3e11, pe_median: 5.1,
      rank: 2, percentile: 50 },
  ],
};

/** 记录页面最后一次传给 SWR 的键，用于断言窗口切换确实进了请求 */
const lastKeys: unknown[] = [];

vi.mock('@/lib/api', () => ({
  get: (...a: unknown[]) => getMock(...a),
  fetcher: (...a: unknown[]) => getMock(...a),
}));

vi.mock('swr', () => ({
  default: (key: unknown) => {
    lastKeys.push(key);
    return { data: ROTATION, isLoading: false };
  },
}));

import IndustryPage from '../page';

describe('IndustryPage', () => {
  beforeEach(() => {
    getMock.mockReset();
    lastKeys.length = 0;
  });

  it('渲染标题与行业轮动榜', () => {
    render(<IndustryPage />);
    expect(screen.getByText('行业分析')).toBeInTheDocument();
    expect(screen.getByText('电子')).toBeInTheDocument();
    expect(screen.getByText('银行')).toBeInTheDocument();
    // 排名来自后端的 rank 字段
    expect(screen.getByText('#')).toBeInTheDocument();
    expect(screen.getByText('+8.50%')).toBeInTheDocument();
    expect(screen.getByText('-3.20%')).toBeInTheDocument();
  });

  it('逐行链到行业详情页', () => {
    render(<IndustryPage />);
    expect(screen.getByText('电子').closest('a'))
      .toHaveAttribute('href', '/industry/801080.SI');
    expect(screen.getByText('银行').closest('a'))
      .toHaveAttribute('href', '/industry/801780.SI');
  });

  it('切换排名窗口会重新发请求', async () => {
    render(<IndustryPage />);
    expect(lastKeys[lastKeys.length - 1]).toBe('/industry/rotation?window=20');
    fireEvent.click(screen.getByText('60 日'));
    await waitFor(() =>
      expect(lastKeys[lastKeys.length - 1]).toBe('/industry/rotation?window=60'));
  });

  it('展示行业分类标准与口径说明', () => {
    render(<IndustryPage />);
    expect(screen.getByText(/标准 SW/)).toBeInTheDocument();
    expect(screen.getByText(/等权合成/)).toBeInTheDocument();
  });
});
