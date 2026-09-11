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

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  get: mockGet,
  post: mockPost,
}));

function mockApi({
  runs = [] as unknown[],
  agents = [] as unknown[],
}: { runs?: unknown[]; agents?: unknown[] } = {}) {
  mockGet.mockImplementation((path: string) => {
    if (path === '/factors/mine/runs') return Promise.resolve(runs);
    if (path === '/factors/agents') return Promise.resolve(agents);
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
  });

  it('无数据时显示空态', async () => {
    mockApi();
    render(<MinePage />);
    expect(await screen.findByText(/未注册 Agent/)).toBeInTheDocument();

    expect(screen.getByText(/还没有挖掘会话/)).toBeInTheDocument();
  });


  it('挖掘成功后展示幸存因子并可一键注册', async () => {
    mockApi({
      agents: [{ name: 'gp-internal', kind: 'builtin', driver: 'python', enabled: true, quota_eval: 500, can_submit: true }],
      runs: [],
    });
    mockPost.mockImplementation((path: string, body: Record<string, unknown>) => {
      if (path === '/factors/mine/run') {
        return Promise.resolve({
          run_id: 'abc123', agent: 'gp-internal', generator: 'random',
          n_evaluated: 20, n_static_fail: 5, n_low_ic: 10, n_redundant: 2,
          n_size_proxy: 1, n_survivors: 2,
          survivors: [
            { expr: 'Rank($close/Mean($close,20))', ic_neutral: 0.05, t_stat: 3.1, origin: 'gp-internal' },
          ],
        });
      }
      if (path === '/factors') return Promise.resolve({});
      return Promise.resolve({});
    });
    renderPage();
    await screen.findAllByText(/gp-internal/);
    fireEvent.click(screen.getByRole('button', { name: /跑 100 个 RANDOM/ }));
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
});
