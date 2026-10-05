import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

/**
 * useFactorEval：评价任务统一发起/跟踪。
 *
 * 重点回归：评价的 job id 是**确定性**的（同因子重跑 = 同一个 id）。只比较 jobId
 * 的订阅不会重连，界面会停在上一轮结果 —— 必须靠 reconnectKey 强制重订阅。
 */

const postMock = vi.fn();

vi.mock('@/lib/api', () => ({ post: (...args: unknown[]) => postMock(...args) }));

/** useJobStream 收到的 (jobId, reconnectKey) 序列 + 可改写的流状态 */
const probe = vi.hoisted(() => ({
  seen: [] as { jobId: string | null; key: number }[],
  state: {
    status: null as string | null,
    progress: null as unknown,
    result: null as unknown,
    error: null as string | null,
    done: false,
  },
}));

vi.mock('@/lib/streaming', () => ({
  useJobStream: (jobId: string | null, reconnectKey = 0) => {
    probe.seen.push({ jobId, key: reconnectKey });
    return probe.state;
  },
}));

import { useFactorEval } from '../useFactorEval';

function resetStream() {
  probe.state.status = null;
  probe.state.progress = null;
  probe.state.result = null;
  probe.state.error = null;
  probe.state.done = false;
}

afterEach(() => {
  vi.clearAllMocks();
  resetStream();
  probe.seen = [];
});

describe('useFactorEval', () => {
  it('start 入队并暴露 jobId；未发起时订阅 null（不连 WS）', async () => {
    postMock.mockResolvedValue({ job_id: 'factor-eval-mom20' });
    const { result, rerender } = renderHook(() => useFactorEval());

    expect(result.current.jobId).toBeNull();
    expect(result.current.running).toBe(false);
    expect(probe.seen[0]).toEqual({ jobId: null, key: 0 });

    await act(async () => {
      await result.current.start({ factor: 'mom20', formula: 'pct_change_20' });
    });

    expect(postMock).toHaveBeenCalledWith('/factors/evaluate', {
      factor: 'mom20',
      formula: 'pct_change_20',
    });
    expect(result.current.jobId).toBe('factor-eval-mom20');
    expect(result.current.running).toBe(true);
  });

  it('同因子重跑（job id 相同）必须靠 reconnectKey 重连', async () => {
    postMock.mockResolvedValue({ job_id: 'factor-eval-mom20' });
    const { result, rerender } = renderHook(() => useFactorEval());

    await act(async () => {
      await result.current.start({ factor: 'mom20', formula: 'pct_change_20' });
    });
    const first = probe.seen[probe.seen.length - 1];
    expect(first.jobId).toBe('factor-eval-mom20');

    await act(async () => {
      await result.current.start({ factor: 'mom20', formula: 'pct_change_20' });
    });
    const second = probe.seen[probe.seen.length - 1];
    // jobId 相同，但 key 必须前进 —— 否则 effect 不重跑，界面停在上一轮
    expect(second.jobId).toBe('factor-eval-mom20');
    expect(second.key).toBeGreaterThan(first.key);
  });

  it('入队失败：startError 作为 failure 透出，jobId 保持 null', async () => {
    postMock.mockRejectedValue(new Error('409 因子评价任务进行中'));
    const { result, rerender } = renderHook(() => useFactorEval());

    await act(async () => {
      await result.current.start({ factor: 'mom20', formula: 'pct_change_20' });
    });

    expect(result.current.jobId).toBeNull();
    expect(result.current.failure).toBe('409 因子评价任务进行中');
    expect(result.current.running).toBe(false);
  });

  it('无 result 的终态（not_found）→ failure 如实说明，不无限运行中', async () => {
    postMock.mockResolvedValue({ job_id: 'factor-eval-x' });
    const { result, rerender } = renderHook(() => useFactorEval());
    await act(async () => {
      await result.current.start({ factor: 'x', formula: 'pct_change_20' });
    });

    act(() => {
      probe.state.status = 'not_found';
      probe.state.done = true;
      rerender();
    });

    expect(result.current.running).toBe(false);
    expect(result.current.failure).toBe('评价任务异常结束（not_found）');
  });

  it('终态 result 到达：running 关闭且结果可读', async () => {
    postMock.mockResolvedValue({ job_id: 'factor-eval-x' });
    const { result, rerender } = renderHook(() => useFactorEval<{ ic: { mean: number } }>());
    await act(async () => {
      await result.current.start({ factor: 'x', formula: 'pct_change_20' });
    });

    act(() => {
      probe.state.status = 'finished';
      probe.state.result = { ic: { mean: 0.03 } };
      probe.state.done = true;
      rerender();
    });

    expect(result.current.running).toBe(false);
    expect(result.current.failure).toBeNull();
    expect(result.current.result?.ic.mean).toBe(0.03);
  });

  it('cancel 打任务中心取消端点；reset 清空 jobId', async () => {
    postMock.mockResolvedValue({ job_id: 'factor-eval-mom20' });
    const { result, rerender } = renderHook(() => useFactorEval());
    await act(async () => {
      await result.current.start({ factor: 'mom20', formula: 'pct_change_20' });
    });

    await act(async () => {
      await result.current.cancel();
    });
    expect(postMock).toHaveBeenCalledWith('/tasks/factor/factor-eval-mom20/cancel', {});

    act(() => result.current.reset());
    expect(result.current.jobId).toBeNull();
  });
});
