// 基本面排名页 —— 排名用归一化分数，覆盖率单列；无数据时显示 hint。
// 另覆盖 P2 补强：行业筛选、列排序、页签（行业分位 / 三表勾稽）、CSV 导出。
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const postMock = vi.fn();
const getMock = vi.fn();

const CATALOGUE = {
  modules: [
    { name: 'profitability', label: '盈利能力', weight: 25, n_metrics: 4 },
    { name: 'cashflow', label: '现金质量', weight: 20, n_metrics: 3 },
    { name: 'efficiency', label: '营运效率', weight: 15, n_metrics: 3 },
    { name: 'solvency', label: '偿债能力', weight: 20, n_metrics: 4 },
    { name: 'valuation', label: '估值水平', weight: 20, n_metrics: 3 },
  ],
  metrics: [
    { item: 'indicator.roe', label: '净资产收益率', module: 'profitability',
      max_score: 8, higher_better: true, direction: '越高越好', source: 'pit' },
    { item: 'derived.cfo_to_np', label: '经营现金流/净利润', module: 'cashflow',
      max_score: 8, higher_better: true, direction: '越高越好', source: 'derived' },
  ],
  sources: { pit: 'financial_pit 报表科目', derived: '由原始报表科目现算' },
};

const INDUSTRIES = {
  rows: [
    { industry: '白酒', n: 20, avg_score: 70.1 },
    { industry: '银行', n: 42, avg_score: 61.2 },
  ],
};

const SCORES = {
  asof: '2026-04-01',
  available: true,
  n_scored: 2,
  n_total: 12,
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
  modules: {
    profitability: { n_metrics: 4, max_score: 25, n_scored_rows: 19, hit_rate: 1 },
    cashflow: { n_metrics: 3, max_score: 20, n_scored_rows: 14, hit_rate: 1 },
    efficiency: { n_metrics: 3, max_score: 15, n_scored_rows: 14, hit_rate: 1 },
    solvency: { n_metrics: 4, max_score: 20, n_scored_rows: 19, hit_rate: 1 },
    valuation: { n_metrics: 3, max_score: 20, n_scored_rows: 0, hit_rate: 0 },
  },
  availability: {
    financial_pit: { available: true, rows: 983856, lookback_years: 5 },
    valuation_lake: { available: false, rows: 0, hint: '日线估值列为空' },
  },
};

const PERCENTILES = {
  asof: '2026-04-01',
  available: true,
  rows: [
    { industry: '白酒', item: 'indicator.roe', label: '净资产收益率',
      module: 'profitability', p25: 10, p50: 15, p75: 20, n: 20 },
    { industry: '银行', item: 'indicator.roe', label: '净资产收益率',
      module: 'profitability', p25: 8, p50: 12, p75: 16, n: 42 },
  ],
};

let scoresResponse: unknown = SCORES;
/** 记录页面最后一次传给 SWR 的键，用于断言筛选/排序确实进了请求 */
const lastKeys: unknown[] = [];

vi.mock('@/lib/api', () => ({
  get: (...a: unknown[]) => getMock(...a),
  post: (...a: unknown[]) => postMock(...a),
  fetcher: (...a: unknown[]) => getMock(...a),
}));

vi.mock('swr', () => ({
  default: (key: unknown) => {
    lastKeys.push(key);
    if (key === '/fundamental/metrics') return { data: CATALOGUE, isLoading: false };
    if (Array.isArray(key) && key[0] === '/fundamental/industries') {
      return { data: INDUSTRIES, isLoading: false };
    }
    if (Array.isArray(key)) return { data: scoresResponse, isLoading: false };
    // 行业分位页签的 GET
    return { data: PERCENTILES, isLoading: false };
  },
}));

// ECharts 在 jsdom 下没有真实布局，渲染成占位即可
vi.mock('echarts-for-react', () => ({ default: () => <div data-testid="chart" /> }));

import FundamentalPage from '../page';

describe('FundamentalPage', () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
    scoresResponse = SCORES;
    lastKeys.length = 0;
  });

  it('渲染标题与筛选控件（含新增行业/样本/搜索）', () => {
    render(<FundamentalPage />);
    expect(screen.getByText('基本面排名')).toBeInTheDocument();
    expect(screen.getByLabelText('最低覆盖率')).toBeInTheDocument();
    expect(screen.getByLabelText('代码搜索')).toBeInTheDocument();
    expect(screen.getByLabelText('最小行业样本')).toBeInTheDocument();
    expect(screen.getByLabelText('观察日')).toBeInTheDocument();
    expect(screen.getByText(/行业相对分位评分/)).toBeInTheDocument();
  });

  it('按归一化分数渲染排名表，并逐行链到个股页', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());

    const link = screen.getByText('600000.SH').closest('a');
    expect(link).toHaveAttribute('href', '/security/600000.SH');
    expect(screen.getByText('100.0')).toBeInTheDocument();
    expect(screen.getByText('66.7')).toBeInTheDocument();
    // 覆盖率单列，与总分并列可见。
    // 逐行断言而不是全局 getByText：页面上还有「评分口径」表与模块命中率条，
    // 它们同样会出现 100% 这类数值。
    const rowA = screen.getByText('600000.SH').closest('tr')!;
    const rowB = screen.getByText('600001.SH').closest('tr')!;
    expect(within(rowA).getByText('100%')).toBeInTheDocument();
    expect(within(rowB).getByText('53%')).toBeInTheDocument();
    expect(screen.getByText('优秀')).toBeInTheDocument();
    expect(screen.getByText('一般')).toBeInTheDocument();
  });

  it('渲染模块列与模块权重口径，并标注取数来源', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    expect(screen.getAllByText('盈利能力').length).toBeGreaterThan(0);
    expect(screen.getByText('25')).toBeInTheDocument();
    expect(screen.getByText('indicator.roe')).toBeInTheDocument();
    expect(screen.getAllByText('越高越好').length).toBeGreaterThan(0);
    expect(screen.getByText('派生')).toBeInTheDocument();
  });

  it('展示各模块可得性，并把无数据源的模块标出来', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    expect(screen.getByText(/无数据源/)).toBeInTheDocument();
  });

  it('展示数据源可用性并给出对应修复指引', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    expect(screen.getByText(/财务报表/)).toBeInTheDocument();
    expect(screen.getAllByText(/日线估值/).length).toBeGreaterThan(0);
    expect(screen.getByText(/估值模块 20 分恒为 0/)).toBeInTheDocument();
  });

  it('行业筛选器由 /fundamental/industries 渲染，点击后进入请求键', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(
      screen.getByRole('button', { name: /银行/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /银行/ }));
    await waitFor(() => {
      const scoreKeys = lastKeys.filter(
        (k) => Array.isArray(k) && k[0] === '/fundamental/scores');
      expect(String(scoreKeys[scoreKeys.length - 1])).toContain('银行');
    });
  });

  it('点击列头改变排序字段', async () => {
    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /覆盖率/ }));
    await waitFor(() => {
      const scoreKeys = lastKeys.filter(
        (k) => Array.isArray(k) && k[0] === '/fundamental/scores');
      expect(String(scoreKeys[scoreKeys.length - 1])).toContain('coverage');
    });
  });

  it('可切到行业分位与三表勾稽页签', async () => {
    render(<FundamentalPage />);
    fireEvent.click(screen.getByRole('button', { name: '行业分位' }));
    await waitFor(() => expect(screen.getByLabelText('分位指标')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '三表勾稽' }));
    await waitFor(() => expect(screen.getByLabelText('勾稽股票代码')).toBeInTheDocument());
  });

  it('导出 CSV 会触发一次下载', async () => {
    const createObjectURL = vi.fn(() => 'blob:x');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    render(<FundamentalPage />);
    await waitFor(() => expect(screen.getByText('600000.SH')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '导出 CSV' }));

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
  });

  it('无数据时显示后端 hint', async () => {
    scoresResponse = { asof: '2026-01-10', available: false,
                       hint: '财务数据为空：先执行 lq data financial', n_scored: 0, rows: [] };
    render(<FundamentalPage />);
    expect(await screen.findByText(/财务数据为空/)).toBeInTheDocument();
  });

  it('筛选后无结果时给出可操作提示', async () => {
    scoresResponse = { asof: '2026-04-01', available: true, n_scored: 0,
                       n_total: 0, rows: [] };
    render(<FundamentalPage />);
    expect(await screen.findByText(/试试降低最低覆盖率/)).toBeInTheDocument();
  });
});
