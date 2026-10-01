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

/** 最小可用 series：仅提供被断言字段，其余图表守卫会自动跳过 */
function series(extra: Record<string, unknown> = {}) {
  return {
    factor: 'edge', formula: 'pct_change_20', n_groups: 5, n_samples: 120,
    ic: { dates: [], ic: [], rank_ic: [], cum_ic: [] },
    quantile: { dates: [], curves: {}, groups: [], monotonicity: null },
    decay: { horizons: [], ic: [], rank_ic: [] },
    ic_by_year: [],
    ...extra,
  };
}

/** 评价基础指标（rating/robustness 等按用例补充） */
function metrics(extra: Record<string, unknown> = {}) {
  return {
    factor: 'edge', formula: 'pct_change_20', n_samples: 120,
    ic: { mean: 0.03, ir: 0.5, t_stat: 2.1, positive_rate: 0.55 },
    rank_ic_mean: 0.02,
    long_short: { annual_return: 0.2, sharpe: 1.1, max_drawdown: -0.1 },
    monotonicity: 0.8, half_life: 10, suggested_rebalance: '5 日',
    excess: { annual_excess: 0.05, excess_sharpe: 0.6, excess_mdd: -0.05 },
    annual_turnover: 1.5, top_n: [], style_corr: { max_abs: 0.1, passed: true },
    report_url: '/api/factors/reports/edge',
    ...extra,
  };
}

/** 点击「运行评价」并等待结果面板出现 */
async function runEval(routes: Record<string, unknown>) {
  const fetchMock = stubPageFetch({
    '/factors/evaluate': { job_id: 'job-1', status: 'queued' },
    ...routes,
  });
  renderPage(<FactorsPage />);
  await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: '运行评价' }));
  await waitFor(() => expect(screen.getByText('评价结果')).toBeInTheDocument());
  return fetchMock;
}

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

  it('评级面板：渲染强/中/弱徽标与理由、未达标项', async () => {
    streamState.result = metrics({
      rating: {
        rating: 'moderate', score: 2, source: '默认阈值',
        ic_mean: 0.03, icir: 0.5, t_stat_nw: 1.8, t_threshold: 2.0,
        significant: false, n_trials: 10, monotonicity: 0.8, ls_sharpe: 1.0,
        reasons: ['|ICIR|=0.5000 ≥ 0.5'],
        blockers: ['|t|=1.8000 < 校正门槛 2.00（n_trials=10）'],
        thresholds: {},
      },
      series: series(),
    });
    await runEval({ '/factors/builtin': [], '/factors/universes': [], '/factors': factors });

    expect(screen.getByLabelText('评级 中')).toBeInTheDocument();
    expect(screen.getByText('|ICIR|=0.5000 ≥ 0.5')).toBeInTheDocument();
    expect(screen.getByText('|t|=1.8000 < 校正门槛 2.00（n_trials=10）')).toBeInTheDocument();
  });

  it('未返回 rating 时评级面板显示空态', async () => {
    streamState.result = metrics({ series: series() });
    await runEval({ '/factors/builtin': [], '/factors/universes': [], '/factors': factors });
    expect(screen.getByText(/暂无评级数据/)).toBeInTheDocument();
  });

  it('稳健性面板：渲染 verdict 与检查项表；缺省时提示为可选项', async () => {
    streamState.result = metrics({ series: series() });
    await runEval({ '/factors/builtin': [], '/factors/universes': [], '/factors': factors });
    // 默认不带 robustness → 提示 opt-in
    expect(screen.getByText(/勾选「稳健性检验」后再跑一次评价/)).toBeInTheDocument();

    streamState.result = metrics({
      robustness: {
        factor: 'edge', n_passed: 1, n_judged: 2, verdict: 'fragile',
        checks: [
          { name: 'time_stability', status: 'failed', value: 0.3, threshold: 0.5, hint: '分段 ICIR 波动过大' },
          { name: 'start_date_sensitivity', status: 'passed', value: 0.1, threshold: 0.3, hint: '起点稳定' },
        ],
      },
      series: series(),
    });
    fireEvent.click(screen.getByRole('button', { name: '运行评价' }));
    await waitFor(() => expect(screen.getByText('时间稳定性')).toBeInTheDocument());
    expect(screen.getByText('未通过')).toBeInTheDocument();
    expect(screen.getByText('分段 ICIR 波动过大')).toBeInTheDocument();
    expect(screen.getByText(/1\/2 项通过/)).toBeInTheDocument();
  });

  it('errors 非空时显示计算失败横幅（不折叠成样本不足）', async () => {
    streamState.result = metrics({
      errors: { top_n: 'ValueError: boom' },
      series: series({ errors: { group_ic: 'KeyError: industry' } }),
    });
    await runEval({ '/factors/builtin': [], '/factors/universes': [], '/factors': factors });

    expect(screen.getByText(/评价过程有计算失败/)).toBeInTheDocument();
    expect(screen.getByText(/ValueError: boom/)).toBeInTheDocument();
    expect(screen.getByText(/KeyError: industry/)).toBeInTheDocument();
  });

  it('neutral_views：显示口径标签与 industry_group_quantile 的真实数字', async () => {
    streamState.result = metrics({
      series: series({
        neutral_views: {
          view: 'factor_neutral (默认: 因子~协变量取残差再算 IC)',
          return_neutral_ic: 0.0123,
          industry_group_quantile: {
            n_groups: 5,
            groups: [
              { q: 1, n: 40, mean_ret: 0.001 },
              { q: 5, n: 42, mean_ret: 0.004 },
            ],
            top_bottom_spread: 0.003,
            monotonicity: 0.9,
            n_obs: 200,
            insufficient: false,
          },
        },
      }),
    });
    await runEval({ '/factors/builtin': [], '/factors/universes': [], '/factors': factors });

    expect(screen.getByText(/当前中性化定义/)).toBeInTheDocument();
    expect(screen.getByText('Q5')).toBeInTheDocument();
    expect(screen.getByText(/首尾差 0.00300/)).toBeInTheDocument();
    expect(screen.getByText(/单调性 0.900/)).toBeInTheDocument();
  });

  it('预处理配方选择器：自定义阶段方法并随请求发送，同时展示实际生效 steps', async () => {
    streamState.result = metrics({
      steps: [{ op: 'winsorize', method: 'mad', n: 5 }],
      series: series(),
    });
    const fetchMock = await runEval({
      '/factors/preprocess/methods': {
        stages: ['winsorize', 'standardize'],
        methods: [
          { name: 'mad', stage: 'winsorize', label: 'MAD 去极值' },
          { name: 'zscore', stage: 'standardize', label: 'Z-Score' },
        ],
        default_recipe: [],
      },
      '/factors/builtin': [],
      '/factors/universes': [],
      '/factors': factors,
    });

    // 实际生效配方可见（来自 metrics.steps）
    expect(screen.getByText(/去极值 · mad/)).toBeInTheDocument();

    // 切到自定义 → 选方法 → 勾选稳健性 → 再跑一次
    fireEvent.change(screen.getByLabelText(/预处理配方/), { target: { value: 'custom' } });
    fireEvent.change(screen.getByLabelText(/去极值/), { target: { value: 'mad' } });
    fireEvent.click(screen.getByLabelText(/稳健性检验/));
    fireEvent.click(screen.getByRole('button', { name: '运行评价' }));

    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter((c) => String(c[0]).includes('/factors/evaluate'));
      expect(calls.length).toBeGreaterThanOrEqual(2);
      const body = JSON.parse(String((calls[calls.length - 1][1] as RequestInit).body));
      expect(body.steps).toEqual([{ op: 'winsorize', method: 'mad' }]);
      expect(body.with_robustness).toBe(true);
    });
  });
});
