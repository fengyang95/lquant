// 基本面排名页 —— 排名用归一化分数，覆盖率单列；无数据时显示 hint
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const postMock = vi.fn();
const getMock = vi.fn();

const CATALOGUE = {
  modules: [
    { name: 'profitability', label: '盈利能力', weight: 25 },
    { name: 'cashflow', label: '现金质量', weight: 20 },
    { name: 'efficiency', label: '营运效率', weight: 15 },
    { name: 'solvency', label: '偿债能力', weight: 20 },
    { name: 'valuation', label: '估值水平', weight: 20 },
  ],
  metrics: [
    { item: 'profit.roeAvg', label: '净资产收益率', module: 'profitability',
      max_score: 8, higher_better: true, direction: '越高越好' },
  ],
};

const SCORES = {
  asof: '2026-04-01',
  available: true,
  n_scored: 2,
  rows: [
    { symbol: '600000.SH', industry: '白酒', n_scored: 17, n_metrics: 17, coverage: 1.0,
      raw_score: 80, available_max: 80, normalized_score: 100, rating: '优秀',
      score_profitability: 25, score_cashflow: 20, score_efficiency: 15,
      score_solvency: 20, score_valuation: 20 },
    { symbol: '600001.SH', industry: '银行', n_scored: 9, n_metrics: 17, coverage: 0.53,
      raw_score: 30, available_max: 45, normalized_score: 66.7, rating: '一般',
      score_profitability: 10, score_cashflow: 8, score_efficiency: 5,
      score_solvency: 4, score_valuation: 3 },
  ],
};

let scoresResponse: unknown = SCORES;

vi.mock('@/lib/api', () => ({
  get: (...a: unknown[]) => getMock(...a),
  post: (...a: unknown[]) => postMock(...a),
}));

vi.mock('swr', () => ({
  default: (key: unknown) => {
    if (key === '/fundamental/metrics') return { data: CATALOGUE, isLoading: false };
    if (Array.isArray(key)) return { data: scoresResponse, isLoading: false };
    return { data: undefined, isLoading: false };
  },
}));

import FundamentalPage from '../page';

describe('FundamentalPage', () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
    scoresResponse = SCORES;
    postMock.mockResolvedValue(SCORES);
  });

  it('渲染标题与筛选控件', () => {
    render(<FundamentalPage />);
    expect(screen.getByText('基本面排名')).toBeInTheDocument();
    expect(screen.getByLabelText('最低覆盖率')).toBeInTheDocument();
    expect(screen.getByText(/行业相对分位评分/)).toBeInTheDocument();
  });

  it('按归一化分数渲染排名表，并逐行链到个股页', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());

    const link = screen.getByText('600000.SH').closest('a');
    expect(link).toHaveAttribute('href', '/security/600000.SH');
    expect(screen.getByText('100.0')).toBeInTheDocument();
    expect(screen.getByText('66.7')).toBeInTheDocument();
    // 覆盖率单列，与总分并列可见
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(screen.getByText('53%')).toBeInTheDocument();
    expect(screen.getByText('优秀')).toBeInTheDocument();
    expect(screen.getByText('一般')).toBeInTheDocument();
  });

  it('渲染模块列与模块权重口径', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    expect(screen.getAllByText('盈利能力').length).toBeGreaterThan(0);
    expect(screen.getByText('25')).toBeInTheDocument();
    expect(screen.getByText('profit.roeAvg')).toBeInTheDocument();
    expect(screen.getByText('越高越好')).toBeInTheDocument();
  });

  it('无数据时显示后端 hint', async () => {
    scoresResponse = { asof: '2026-01-10', available: false,
                       hint: '财务数据为空：先执行 lq data financial', n_scored: 0, rows: [] };
    render(<FundamentalPage />);
    expect(await screen.findByText(/财务数据为空/)).toBeInTheDocument();
  });

  it('筛选后无结果时给出可操作提示', async () => {
    scoresResponse = { asof: '2026-04-01', available: true, n_scored: 0, rows: [] };
    render(<FundamentalPage />);
    expect(await screen.findByText(/试试降低最低覆盖率/)).toBeInTheDocument();
  });
});
