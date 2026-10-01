import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import FactorsPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

/** 任务流状态可在测试里改写（vi.hoisted：mock 工厂会被提升到模块顶部）。 */
const streamState = vi.hoisted(() => ({
  error: null as string | null,
  result: null as unknown,
  done: false,
  status: null as string | null,
  progress: null as unknown,
}));

vi.mock('@/lib/streaming', () => ({ useJobStream: () => streamState }));

const factors = [
  {
    name: 'pct_change_20', expression: 'pct_change(close,20)', description: '20日动量',
    created_at: '2026-09-01', source: 'manual', ic_neutral: 0.03, category: '动量',
  },
  {
    name: 'rolling_std_20', expression: 'rolling_std(close,20)', description: '20日波动',
    created_at: '2026-09-02', source: 'qlib', ic_neutral: -0.01, category: '波动',
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
  streamState.result = null;
  streamState.error = null;
  streamState.done = false;
});

describe('FactorsPage', () => {
  it('正常数据：渲染因子页与内置因子计数', async () => {
    stubPageFetch({
      '/factors/builtin': [
        { name: 'alpha101', expression: 'x', description: 'd', created_at: '2026-01-01' },
        { name: 'alpha102', expression: 'x', description: 'd', created_at: '2026-01-02' },
      ],
      '/factors/universes': [{ key: 'all', index_code: null, label: '全市场' }],
      '/factors': factors,
    });
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
    expect(screen.getByText(/内置 Qlib Alpha158 2 个/)).toBeInTheDocument();
    // 快速评价表单壳（tab 与标题同名，断言存在即可）
    expect(screen.getAllByText('快速评价').length).toBeGreaterThan(0);
  });

  it('fetch 失败：壳仍在，不崩溃', async () => {
    stubPageFetch({});
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
    expect(screen.getAllByText('快速评价').length).toBeGreaterThan(0);
  });

  it('切换到因子库 tab：渲染已注册因子', async () => {
    stubPageFetch({
      '/factors/builtin': [],
      '/factors/universes': [],
      '/factors': factors,
    });
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子库')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '因子库' }));
    await waitFor(() => expect(screen.getByText('pct_change_20')).toBeInTheDocument());
  });

  it('评价指标为 null 时渲染 —，不退化成 0.00%', async () => {
    // 后端把非有限值统一转 null（_jf）：null 参与 `null * 100` 会算成 0，
    // 「多空年化」被打成 0.00%（看起来像「收益恰好为 0」）
    streamState.result = {
      factor: 'edge', n_samples: 120,
      ic: { mean: null, ir: null, t_stat: null, positive_rate: null },
      rank_ic_mean: null,
      long_short: { annual_return: null, sharpe: null, max_drawdown: null },
      monotonicity: null, half_life: null, suggested_rebalance: 'unknown',
      excess: { annual_excess: null, excess_sharpe: null, excess_mdd: null },
      annual_turnover: null, top_n: [], style_corr: { max_abs: null, passed: null },
      report_url: '/api/factors/reports/edge',
    };
    stubPageFetch({
      // 顺序敏感：stubPageFetch 按声明序做子串匹配，'/factors/evaluate' 必须先于 '/factors'
      '/factors/evaluate': { job_id: 'factor-eval-edge', status: 'queued' },
      '/factors/builtin': [],
      '/factors/universes': [],
      '/factors': factors,
    });
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运行评价' }));
    await waitFor(() => expect(screen.getByText('评价结果')).toBeInTheDocument());

    expect(screen.getByText('多空年化')).toBeInTheDocument();
    expect(screen.queryByText('0.00%')).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
