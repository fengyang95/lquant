import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SWRConfig } from 'swr';

import MinePage from './page';

/** 每个用例独立 SWR 缓存，避免用例间串扰 */
function renderPage() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MinePage />
    </SWRConfig>,
  );
}

const { mockGet, mockPost, mockUseJobStream } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockUseJobStream: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  get: mockGet,
  post: mockPost,
}));

vi.mock('@/lib/streaming', () => ({
  useJobStream: mockUseJobStream,
}));

const IDLE = { status: null, progress: null, result: null, error: null, done: false };

const MINE_RESULT = {
  run_id: 'abc123', agent: 'gp-internal', generator: 'random',
  n_evaluated: 20, n_static_fail: 5, n_low_ic: 10, n_redundant: 2,
  n_size_proxy: 1, n_survivors: 1,
  survivors: [
    { expr: 'Rank($close/Mean($close,20))', ic_neutral: 0.05, t_stat: 3.1, origin: 'gp-internal' },
  ],
};

function mockApi({
  runs = [] as unknown[],
  agents = [] as unknown[],
}: { runs?: unknown[]; agents?: unknown[] } = {}) {
  mockGet.mockImplementation((path: string) => {
    if (path === '/factors/mine/runs') return Promise.resolve(runs);
    if (path === '/factors/agents') return Promise.resolve(agents);
    if (path.startsWith('/factors/mine/runs/')) {
      return Promise.resolve({
        run_id: 'r1', agent: 'gp-internal', generator: 'gp',
        n_evaluated: 10, n_static_fail: 1, n_low_ic: 2, n_redundant: 0,
        n_size_proxy: 0, n_survivors: 1, created_at: '2026-09-25 08:00:00',
        corrections: { 'Mean($close,20)': 'Mean($close,20)_c1' },
      });
    }
    if (path.startsWith('/factors/agents/')) {
      return Promise.resolve({
        agent: 'gp-internal', kind: 'builtin', driver: 'python', quota_eval: 500,
        can_submit: true, steps: ['步骤一', '步骤二'], acceptance: '全部门禁通过',
      });
    }
    return Promise.resolve([]);
  });
}

describe('FactorsMinePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseJobStream.mockReturnValue(IDLE);
  });

  it('无数据时显示空态', async () => {
    mockApi();
    render(<MinePage />);
    expect(await screen.findByText(/未注册 Agent/)).toBeInTheDocument();

    expect(screen.getByText(/还没有挖掘会话/)).toBeInTheDocument();
  });


  it('异步挖掘：202 入队后经任务流回流幸存因子并可一键注册', async () => {
    mockApi({
      agents: [{ name: 'gp-internal', kind: 'builtin', driver: 'python', enabled: true, quota_eval: 500, can_submit: true }],
      runs: [],
    });
    mockPost.mockImplementation((path: string) => {
      if (path === '/factors/mine/run') {
        return Promise.resolve({ status: 'queued', task_id: 'abc123' });
      }
      return Promise.resolve({});
    });
    // 入队后（jobId 非空）任务流返回终态 result
    mockUseJobStream.mockImplementation((jobId: string | null) => (
      jobId === 'abc123' ? { ...IDLE, status: 'finished', result: MINE_RESULT, done: true } : IDLE
    ));
    renderPage();
    await screen.findAllByText(/gp-internal/);
    fireEvent.click(screen.getByRole('button', { name: /跑 100 个 RANDOM/ }));
    expect(mockPost).toHaveBeenCalledWith('/factors/mine/run',
      { agent: 'gp-internal', generator: 'random', n: 100, sync: false });
    expect(await screen.findByText(/幸存因子 · run abc123/)).toBeInTheDocument();
    expect(screen.getByText(/Rank\(\$close\/Mean\(\$close,20\)\)/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '注册' }));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/factors', {
        name: 'mine_abc123_0', expression: 'Rank($close/Mean($close,20))',
        description: '挖掘幸存 @abc123 #0',
      });
    });
  });

  it('任务流报错时消息条显式提示', async () => {
    mockApi({
      agents: [{ name: 'gp-internal', kind: 'builtin', driver: 'python', enabled: true, quota_eval: 500, can_submit: true }],
      runs: [],
    });
    mockPost.mockResolvedValue({ status: 'queued', task_id: 'abc123' });
    mockUseJobStream.mockImplementation((jobId: string | null) => (
      jobId === 'abc123' ? { ...IDLE, error: '配额不足', done: true } : IDLE
    ));
    renderPage();
    await screen.findAllByText(/gp-internal/);
    fireEvent.click(screen.getByRole('button', { name: /跑 100 个 GP/ }));
    expect(await screen.findByText(/✗ 配额不足/)).toBeInTheDocument();
  });

  it('agent guide 加载并展示验收指引', async () => {
    mockApi({
      agents: [{ name: 'gp-internal', kind: 'builtin', driver: 'python', enabled: true, quota_eval: 500, can_submit: true }],
      runs: [],
    });
    renderPage();
    await screen.findAllByText(/gp-internal/);
    fireEvent.click(screen.getByTitle('查看验收指引'));
    expect(await screen.findByText(/验收指引/)).toBeInTheDocument();
    expect(screen.getByText(/验收标准/)).toBeInTheDocument();
  });

  it('点击台账行加载会话详情与修正日志', async () => {
    mockApi({
      agents: [{ name: 'gp-internal', kind: 'builtin', driver: 'python', enabled: true, quota_eval: 500, can_submit: true }],
      runs: [{ run_id: 'r1', agent: 'gp-internal', generator: 'gp', n_evaluated: 10,
        n_static_fail: 1, n_low_ic: 2, n_redundant: 0, n_size_proxy: 0,
        n_survivors: 1, created_at: '2026-09-25 08:00:00' }],
    });
    renderPage();
    fireEvent.click(await screen.findByText('r1'));
    expect(await screen.findByText(/修正日志/)).toBeInTheDocument();
    expect(screen.getByText(/Mean\(\$close,20\) → Mean\(\$close,20\)_c1/)).toBeInTheDocument();
  });
});
