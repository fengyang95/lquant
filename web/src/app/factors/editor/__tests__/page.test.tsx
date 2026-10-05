import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderPage, stubPageFetch } from '@/test/page-utils';

/**
 * 因子编辑页「保存并评价」链路。
 *
 * 回归重点：本页曾是四处评价入口里唯一用 REST 轮询的一处，且把后端信封
 * `{job_id,ts,kind,params,result}` 当扁平结构读、字段名也错（ic_mean/icir）——
 * 结果面板恒显示 `—`、累计 IC 图恒为空却没有任何测试发现。现改用与因子库/
 * 详情页同一 WS 机制，这里用真实 WS 帧形状（扁平 metrics）锁住读取路径。
 */

/** 任务流状态可在测试里改写（vi.hoisted：mock 工厂会被提升到模块顶部）。 */
const streamState = vi.hoisted(() => ({
  status: null as string | null,
  progress: null as unknown,
  result: null as unknown,
  error: null as string | null,
  done: false,
}));

vi.mock('@/lib/streaming', () => ({ useJobStream: () => streamState }));

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

/** 画布三栏与打开对话框都不参与本测试，mock 掉避免拉进 reactflow / SWR。 */
vi.mock('../Canvas', () => ({ default: () => <div data-testid="canvas-stub" /> }));
vi.mock('../BlockPalette', () => ({ default: () => <div /> }));
vi.mock('../Inspector', () => ({ default: () => <div /> }));
vi.mock('../OpenFactorDialog', () => ({ default: () => null }));

vi.mock('../useFactorEditor', () => ({
  useFactorEditor: () => ({
    catalog: { ops: [] },
    catalogError: '',
    state: { nodes: [], edges: [], selectedId: null },
    compiled: { expression: 'pct_change_20', warnings: [] },
    validation: { ok: true, expression: 'pct_change_20', error: null },
    checking: false,
    selected: null,
    blockingWarnings: [],
    canSave: true,
    addBlock: vi.fn(),
    removeBlock: vi.fn(),
    move: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
    setParam: vi.fn(),
    patchNode: vi.fn(),
    select: vi.fn(),
    reset: vi.fn(),
    loadExpression: vi.fn(),
    markSaved: vi.fn(),
  }),
}));

import FactorEditorPage from '../page';

/** 后端 `_evaluate_full` / WS 终态 result 的真实形状（扁平，非信封）。 */
function metrics(extra: Record<string, unknown> = {}) {
  return {
    factor: 'edge',
    formula: 'pct_change_20',
    n_samples: 120,
    ic: { mean: 0.0312, ir: 0.54, t_stat: 2.1, positive_rate: 0.55 },
    rank_ic_mean: 0.0234,
    report_url: '/api/factors/reports/edge',
    series: {
      ic: { dates: ['2024-01-02', '2024-01-03'], ic: [0.01, 0.02], cum_ic: [0.01, 0.03] },
    },
    ...extra,
  };
}

/** 填名字 → 点「保存并评价」，返回 fetch mock。 */
async function startEval() {
  const fetchMock = stubPageFetch({
    // 顺序敏感：stubPageFetch 按声明序做子串匹配，evaluate 必须先于 /factors
    '/factors/evaluate': { job_id: 'factor-eval-edge', status: 'queued' },
    '/factors': { ok: true },
  });
  renderPage(<FactorEditorPage />);
  fireEvent.change(screen.getByPlaceholderText('因子名（字母/下划线开头）'), {
    target: { value: 'edge' },
  });
  const btn = screen.getByRole('button', { name: '保存并评价' });
  await waitFor(() => expect(btn).toBeEnabled());
  fireEvent.click(btn);
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some((c) => String(c[0]).includes('/factors/evaluate')),
    ).toBe(true),
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  streamState.status = null;
  streamState.progress = null;
  streamState.result = null;
  streamState.error = null;
  streamState.done = false;
});

describe('FactorEditorPage 保存并评价', () => {
  it('运行中：渲染进度条与阶段文案', async () => {
    streamState.status = 'started';
    streamState.progress = { done: 50, total: 100, phase: '评价计算' };
    await startEval();

    await waitFor(() => expect(screen.getByText(/运行中/)).toBeInTheDocument());
    expect(screen.getByText('评价计算')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
  });

  it('终态 result：按后端真实字段渲染 IC/ICIR 与报告链接', async () => {
    streamState.status = 'finished';
    streamState.result = metrics();
    streamState.done = true;
    await startEval();

    await waitFor(() => expect(screen.getByText('IC 均值')).toBeInTheDocument());
    // 字段名回归防线：ic.mean / ic.ir / rank_ic_mean（不是 ic_mean / icir）
    expect(screen.getByText('0.0312')).toBeInTheDocument();
    expect(screen.getByText('0.0234')).toBeInTheDocument();
    expect(screen.getByText('0.5400')).toBeInTheDocument();
    // 到达结果后不再显示运行中
    expect(screen.queryByText(/运行中/)).toBeNull();
    // 累计 IC 图：series 非空即可出图
    expect(screen.getByTestId('echarts-stub')).toBeInTheDocument();
    // 报告外链（本页此前没有）
    const link = screen.getByRole('link', { name: /查看完整报告/ });
    expect(link).toHaveAttribute('href', '/api/factors/reports/edge');
  });

  it('指标为 null：显示 — 而不是 0.0000', async () => {
    streamState.status = 'finished';
    streamState.result = metrics({
      ic: { mean: null, ir: null },
      rank_ic_mean: null,
      report_url: null,
      series: null,
    });
    streamState.done = true;
    await startEval();

    await waitFor(() => expect(screen.getByText('IC 均值')).toBeInTheDocument());
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText('0.0000')).toBeNull();
    expect(screen.queryByRole('link', { name: /查看完整报告/ })).toBeNull();
  });

  it('任务失败：透出 WS error，不无限"运行中"', async () => {
    streamState.status = 'failed';
    streamState.error = 'JobCanceled: 因子评价已取消: edge';
    streamState.done = true;
    await startEval();

    await waitFor(() => expect(screen.getByText(/评价失败/)).toBeInTheDocument());
    expect(screen.getByText(/JobCanceled/)).toBeInTheDocument();
  });

  it('无 result 的终态（not_found）：如实报异常结束', async () => {
    streamState.status = 'not_found';
    streamState.done = true;
    await startEval();

    await waitFor(() =>
      expect(screen.getByText(/评价任务异常结束（not_found）/)).toBeInTheDocument(),
    );
  });
});
