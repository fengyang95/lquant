// 基本面评分卡 —— 总分必须与覆盖率同时出现；不可用时显示 hint 而不是空白
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const swrState: { data: unknown; isLoading: boolean; error: unknown } = {
  data: undefined, isLoading: false, error: undefined,
};

// 评分卡现在有**两个** SWR 请求（/fundamental/score 与 /fundamental/metrics）。
// 按 key 分发，否则模块口径会取到评分的返回体，模块名与满分全部对不上。
const CATALOGUE = {
  modules: [
    { name: 'profitability', label: '盈利能力', weight: 25 },
    { name: 'cashflow', label: '现金质量', weight: 20 },
    { name: 'efficiency', label: '营运效率', weight: 15 },
    { name: 'solvency', label: '偿债能力', weight: 20 },
    { name: 'valuation', label: '估值水平', weight: 20 },
  ],
  metrics: [],
};

vi.mock('swr', () => ({
  default: (key: unknown) => (key === '/fundamental/metrics'
    ? { data: CATALOGUE, isLoading: false }
    : swrState),
}));

import FundamentalCard, { type FundScore } from '../FundamentalCard';

function payload(overrides: Partial<FundScore['score']> = {},
                 available = true): FundScore {
  return {
    symbol: '600519.SH',
    asof: '2026-04-01',
    available,
    hint: available ? undefined : '财务数据为空：先执行 lq data financial',
    score: available ? {
      symbol: '600519.SH', industry: '白酒',
      n_scored: 14, n_metrics: 17, coverage: 14 / 17,
      raw_score: 58.0, available_max: 80.0, normalized_score: 72.5,
      rating: '良好', score_profitability: 18.0, score_cashflow: 12.0,
      score_efficiency: 9.0, score_solvency: 11.0, score_valuation: 8.0,
      ...overrides,
    } as unknown as FundScore['score'] : null,
    items: available ? [{
      item: 'indicator.roe', label: '净资产收益率', module: 'profitability',
      value: 22.5, p25: 10, p50: 15, p75: 20, n: 42,
      ratio: 1.0, points: 8, max_score: 8,
    }] : [],
  };
}

describe('FundamentalCard', () => {
  beforeEach(() => {
    swrState.data = undefined;
    swrState.isLoading = false;
    swrState.error = undefined;
  });

  it('加载中显示 Loading', () => {
    swrState.isLoading = true;
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText('加载中…')).toBeInTheDocument();
  });

  it('请求失败显示错误', () => {
    swrState.error = new Error('boom');
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText(/基本面加载失败/)).toBeInTheDocument();
  });

  it('无数据时显示后端给的 hint（而不是空白）', () => {
    swrState.data = payload({}, false);
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText(/财务数据为空/)).toBeInTheDocument();
  });

  it('有数据时展示评分、评级、行业与覆盖率', () => {
    swrState.data = payload();
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText('72.5')).toBeInTheDocument();
    expect(screen.getByText('良好')).toBeInTheDocument();
    expect(screen.getByText('白酒')).toBeInTheDocument();
    expect(screen.getByText(/82%（14\/17）/)).toBeInTheDocument();
    expect(screen.getByText('2026-04-01')).toBeInTheDocument();
  });

  it('展示模块分解与逐指标明细（含行业分位）', () => {
    swrState.data = payload();
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText('盈利能力 / 25')).toBeInTheDocument();
    expect(screen.getByText('估值水平 / 20')).toBeInTheDocument();
    expect(screen.getByText('净资产收益率')).toBeInTheDocument();
    expect(screen.getByText('indicator.roe')).toBeInTheDocument();
    expect(screen.getByText(/n=42/)).toBeInTheDocument();
  });

  it('覆盖率低于 80% 时给出可信度提示', () => {
    swrState.data = payload({ coverage: 0.3, n_scored: 5 });
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText(/覆盖率偏低/)).toBeInTheDocument();
  });

  it('覆盖率充足时不显示提示', () => {
    swrState.data = payload({ coverage: 0.95, n_scored: 16 });
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.queryByText(/覆盖率偏低/)).not.toBeInTheDocument();
  });

  it('分位区间退化时不给误导性的位置条', () => {
    const p = payload();
    p.items = [{ ...p.items[0], p25: 5, p50: 5, p75: 5 }];
    swrState.data = p;
    render(<FundamentalCard symbol="600519.SH" />);
    expect(screen.getByText('区间退化')).toBeInTheDocument();
  });
});
