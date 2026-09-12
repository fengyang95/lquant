import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CollectBar } from '../CollectBar';
import * as lib from '../lib';

vi.mock('../lib', async () => {
  const actual = await vi.importActual<typeof import('../lib')>('../lib');
  return { ...actual, triggerTask: vi.fn() };
});

beforeEach(() => {
  vi.mocked(lib.triggerTask).mockReset();
});

describe('CollectBar', () => {
  it('采集快讯：只发 FAST_SOURCES，完成后回显条数并回调 onDone', async () => {
    (lib.triggerTask as Mock).mockResolvedValue({
      task_id: 't1',
      status: 'ok',
      rows_written: 12,
    });
    const onDone = vi.fn();
    render(<CollectBar onDone={onDone} />);

    await userEvent.click(screen.getByRole('button', { name: '采集快讯' }));

    expect(lib.triggerTask).toHaveBeenCalledWith({ sources: [...lib.FAST_SOURCES] });
    await waitFor(() => expect(screen.getByText(/新增 12 条/)).toBeInTheDocument());
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('全量采集：不带 sources（后端默认全注册表），回调照常触发', async () => {
    (lib.triggerTask as Mock).mockResolvedValue({
      task_id: 't2',
      status: 'partial',
      rows_written: 5,
    });
    const onDone = vi.fn();
    render(<CollectBar onDone={onDone} />);

    await userEvent.click(screen.getByRole('button', { name: '全量采集' }));

    expect(lib.triggerTask).toHaveBeenCalledWith({});
    await waitFor(() => expect(screen.getByText(/新增 5 条/)).toBeInTheDocument());
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('采集失败：回显错误文案且不回调', async () => {
    (lib.triggerTask as Mock).mockRejectedValue(new Error('another news task is pending'));
    const onDone = vi.fn();
    render(<CollectBar onDone={onDone} />);

    await userEvent.click(screen.getByRole('button', { name: '采集快讯' }));

    await waitFor(() =>
      expect(screen.getByText(/采集失败：another news task is pending/)).toBeInTheDocument(),
    );
    expect(onDone).not.toHaveBeenCalled();
  });

  it('在途期间两个按钮都禁用（同步请求防重复点击）', async () => {
    (lib.triggerTask as Mock).mockReturnValue(new Promise(() => {}));
    render(<CollectBar />);

    await userEvent.click(screen.getByRole('button', { name: '采集快讯' }));

    expect(screen.getByRole('button', { name: '采集中…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '全量采集' })).toBeDisabled();
  });
});
