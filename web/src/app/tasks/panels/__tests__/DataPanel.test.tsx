// DataPanel 测试 —— 列表渲染、失败明细展开、立即增量、重试弹窗
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const useSWRMock = vi.fn();

vi.mock('swr', () => ({
  __esModule: true,
  default: (...args: unknown[]) => useSWRMock(...args),
}));

import DataPanel from '../DataPanel';
import type { DataTask } from '@/app/data/types';

const postMock = vi.fn();

vi.mock('@/lib/api', () => ({
  fetcher: vi.fn(),
  post: (...args: unknown[]) => postMock(...args),
}));

vi.mock('@/app/data/BackfillModal', () => ({
  __esModule: true,
  default: () => <div data-testid="backfill-stub" />,
}));

const okTask = {
  task_id: 'aaaaaaaa-0000-0000-0000-000000000000',
  kind: 'daily_update',
  params: { days: 10 },
  status: 'ok',
  phase: null,
  total_symbols: 5,
  done_symbols: 5,
  failed_symbols: [],
  failed_detail: [],
  rows_written: 120,
  started_at: null,
  finished_at: null,
  message: null,
} as unknown as DataTask;

const failedTask = {
  task_id: 'bbbbbbbb-0000-0000-0000-000000000000',
  kind: 'full_backfill',
  params: { start: '2020-01-01', end: '2020-12-31' },
  status: 'failed',
  phase: 'fetch',
  total_symbols: 4,
  done_symbols: 2,
  failed_symbols: ['600000'],
  failed_detail: [{ symbol: '600000', reason: 'no data' }],
  rows_written: 200,
  started_at: null,
  finished_at: null,
  message: '部分标的失败',
} as unknown as DataTask;

function setup(tasks: DataTask[] | undefined) {
  useSWRMock.mockReset();
  useSWRMock.mockReturnValue({ data: tasks, mutate: vi.fn() });
  render(<DataPanel />);
}

describe('DataPanel', () => {
  beforeEach(() => {
    postMock.mockReset();
    postMock.mockResolvedValue({ task_id: '12345678-abcd-abcd-abcd-abcdefabcdef' });
  });

  it('经 useSWR(\'/data/tasks?limit=50\', fetcher) 取数', () => {
    setup([okTask]);
    expect(useSWRMock).toHaveBeenCalledWith('/data/tasks?limit=50', expect.anything(), expect.anything());
  });

  it('渲染任务行：类型文案 / id 前 8 位 / 进度与 phase', () => {
    setup([okTask]);
    expect(screen.getByText('每日增量')).toBeInTheDocument();
    expect(screen.getByText('aaaaaaaa')).toBeInTheDocument();
    expect(screen.getByText('days=10')).toBeInTheDocument();
    expect(screen.getByText('5/5 标的')).toBeInTheDocument();
  });

  it('空列表 → 空态文案指向右上角入口', () => {
    setup([]);
    expect(screen.getByText(/暂无任务 —— 点右上角/)).toBeInTheDocument();
  });

  it('failed 任务展开失败明细表', async () => {
    setup([failedTask]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '展开失败明细' }));
    expect(screen.getByText('600000')).toBeInTheDocument();
    expect(screen.getByText('no data')).toBeInTheDocument();
  });

  it('点「立即增量」→ POST /data/tasks {kind: daily_update}', async () => {
    setup([]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '立即增量' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/data/tasks', {
        kind: 'daily_update',
        params: { days: 10 },
      });
    });
    expect(screen.getByText(/✓ 增量任务已创建（12345678…）/)).toBeInTheDocument();
  });

  it('点「全量回填」打开 BackfillModal（modal 本体行为由其自有测试覆盖）', async () => {
    setup([]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '全量回填' }));
    expect(screen.getByTestId('backfill-stub')).toBeInTheDocument();
  });

  it('重试弹窗：默认预填原 params，非法 JSON 报错，合法 JSON POST retry', async () => {
    setup([failedTask]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '重试' }));
    const box = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(JSON.parse(box.value)).toEqual({ start: '2020-01-01', end: '2020-12-31' });

    await user.clear(box);
    fireEvent.change(box, { target: { value: '{bad json' } });
    await user.click(screen.getByRole('button', { name: '创建重试任务' }));
    expect(screen.getByText('✗ 参数不是合法 JSON')).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();

    await user.clear(box);
    fireEvent.change(box, { target: { value: '{"start":"2021-01-01"}' } });
    await user.click(screen.getByRole('button', { name: '创建重试任务' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/tasks/data/bbbbbbbb-0000-0000-0000-000000000000/retry', {
        params: { start: '2021-01-01' },
      });
    });
    expect(screen.getByText(/✓ 重试任务已创建（12345678…）/)).toBeInTheDocument();
  });
});

// 数据任务取消 —— 与 ml/backtest/qlib/factor 四个兄弟面板对齐的能力
// （原先只有 /data 的 TasksPanel 有，已合并进本面板）
describe('DataPanel 任务取消', () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  const runningTask = {
    task_id: 'cccccccc-0000-0000-0000-000000000000',
    kind: 'full_backfill',
    params: { start: '2016-01-01', end: '2026-09-12' },
    status: 'running',
    phase: 'fetch',
    total_symbols: 100,
    done_symbols: 40,
    failed_symbols: [],
    failed_detail: [],
    rows_written: 4000,
    started_at: null,
    finished_at: null,
    message: null,
  } as unknown as DataTask;

  it('running 任务显示「取消」按钮，点击 POST /tasks/data/{id}/cancel', async () => {
    postMock.mockResolvedValue({ canceled: true });
    setup([runningTask]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /取消任务 cccccccc/ }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        '/tasks/data/cccccccc-0000-0000-0000-000000000000/cancel',
        {},
      );
    });
    expect(screen.getByText(/✓ 已请求取消（cccccccc…）/)).toBeInTheDocument();
  });

  it('409 竞态（任务已结束）→ 友好提示而非报错', async () => {
    postMock.mockRejectedValue(new Error('409 Conflict: 任务已结束，无法取消'));
    setup([runningTask]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /取消任务 cccccccc/ }));
    expect(await screen.findByText(/任务已结束，无需取消（cccccccc…）/)).toBeInTheDocument();
  });

  it('failed 任务无取消按钮', () => {
    setup([failedTask]);
    expect(screen.queryByRole('button', { name: /取消任务/ })).not.toBeInTheDocument();
  });
});
