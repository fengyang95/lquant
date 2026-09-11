// FactorPanel 测试 —— 发起校验（预算 n）、POST /factors/mine/run 请求体、挖掘台账渲染
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const useSWRMock = vi.fn();

vi.mock('swr', () => ({
  __esModule: true,
  default: (...args: unknown[]) => useSWRMock(...args),
}));

const postMock = vi.fn();

vi.mock('@/lib/api', () => ({
  fetcher: vi.fn(),
  get: vi.fn(),
  post: (...args: unknown[]) => postMock(...args),
}));

import FactorPanel from '../FactorPanel';
import type { TaskItem } from '../../types';

const task: TaskItem = {
  id: 'aaaaaaaa-0000-0000-0000-000000000000',
  kind: 'factor',
  name: 'gp-internal / random',
  status: 'running',
  state: 'running',
  created_at: '2024-06-01T10:30:45',
  params: { agent: 'gp-internal', generator: 'random', n: 100 },
  error: null,
};

const run = {
  run_id: 'run-1',
  agent: 'gp-internal',
  generator: 'random',
  n_evaluated: 100,
  n_static_fail: 20,
  n_low_ic: 10,
  n_redundant: 8,
  n_size_proxy: 2,
  n_survivors: 60,
  created_at: '2024-06-01T10:30:45',
};

function setup(tasks: TaskItem[] | undefined, runs: unknown[] | undefined) {
  useSWRMock.mockImplementation((key: string) => {
    if (key === '/tasks?kind=factor') return { data: tasks, isLoading: false, mutate: vi.fn() };
    if (key === '/factors/mine/runs') return { data: runs, mutate: vi.fn() };
    return { data: undefined, mutate: vi.fn() };
  });
  render(<FactorPanel />);
}

describe('FactorPanel', () => {
  beforeEach(() => {
    useSWRMock.mockReset();
    useSWRMock.mockImplementation(() => ({ data: undefined, mutate: vi.fn() }));
    postMock.mockReset();
    postMock.mockResolvedValue({ task_id: '12345678-abcd-abcd-abcd-abcdefabcdef', status: 'queued' });
  });

  it('取两个 SWR key：任务列表与挖掘台账', () => {
    useSWRMock.mockImplementation((key: string) => ({ data: undefined, mutate: vi.fn() }));
    setup(undefined, undefined);
    expect(useSWRMock).toHaveBeenCalledWith('/tasks?kind=factor', expect.anything(), expect.anything());
    expect(useSWRMock).toHaveBeenCalledWith('/factors/mine/runs', expect.anything());
  });

  it('渲染任务与台账行（漏斗各列）', () => {
    setup([task], [run]);
    expect(screen.getByText('gp-internal / random')).toBeInTheDocument();
    expect(screen.getByText('run-1')).toBeInTheDocument();
    expect(screen.getByText('60')).toBeInTheDocument();
    expect(screen.queryByText('还没有挖掘会话 —— 上方发起一次')).not.toBeInTheDocument();
  });

  it('台账为空 → 空态文案', () => {
    setup([], []);
    expect(screen.getByText('还没有挖掘会话 —— 上方发起一次')).toBeInTheDocument();
  });

  it('预算 n 非法（0/负数）→ 校验报错且不发请求', async () => {
    setup(undefined, undefined);
    const user = userEvent.setup();
    const nInput = screen.getByLabelText(/预算 n/);
    await user.clear(nInput);
    await user.type(nInput, '0');
    await user.click(screen.getByRole('button', { name: '发起挖掘' }));
    expect(screen.getByText('✗ 请填写 Agent、生成器和正整数预算 n')).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it('填全表单 → POST /factors/mine/run 携带 agent/generator/n，成功透出排队反馈', async () => {
    setup(undefined, undefined);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '发起挖掘' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/factors/mine/run', {
        agent: 'gp-internal',
        generator: 'random',
        n: 100,
      });
    });
    expect(screen.getByText(/✓ 挖掘任务已排队（12345678…，queued）/)).toBeInTheDocument();
  });

  it('发起失败 → 透出 ✗ 错误反馈', async () => {
    postMock.mockRejectedValue(new Error('409 conflict'));
    setup(undefined, undefined);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '发起挖掘' }));
    await waitFor(() => {
      expect(screen.getByText('✗ 409 conflict')).toBeInTheDocument();
    });
  });
});
