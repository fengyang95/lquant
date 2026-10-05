import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useJobStream } from '../streaming';

/** 每次构造返回新实例：重连会 new 多个 WebSocket */
function makeWsMock() {
  const instances: Array<{
    onmessage: ((ev: { data: string }) => void) | null;
    onclose: (() => void) | null;
    close: ReturnType<typeof vi.fn>;
  }> = [];
  const Ctor = vi.fn().mockImplementation(() => {
    const inst = {
      onmessage: null as ((ev: { data: string }) => void) | null,
      onclose: null as (() => void) | null,
      close: vi.fn(),
    };
    instances.push(inst);
    return inst;
  });
  return { Ctor, instances };
}

describe('useJobStream', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('interrupted 终态帧 → error 提示重新发起，done=true', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-1'));
    act(() => {
      instances[0].onmessage?.({ data: JSON.stringify({ status: 'interrupted', done: true }) });
    });
    expect(result.current.done).toBe(true);
    expect(result.current.error).toContain('已中断');
  });

  it('显式 error 帧优先于 interrupted 兜底文案', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-2'));
    act(() => {
      instances[0].onmessage?.({ data: JSON.stringify({ status: 'failed', error: '行情源不可用', done: true }) });
    });
    expect(result.current.error).toBe('行情源不可用');
  });

  it('终态后 onclose 不覆盖既有状态', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-3'));
    act(() => {
      instances[0].onmessage?.({ data: JSON.stringify({ status: 'interrupted', done: true }) });
      instances[0].onclose?.();
    });
    expect(result.current.done).toBe(true);
    expect(result.current.error).toContain('已中断');
  });

  it('终态帧后服务端关连 → 正常收尾，不再发起重连', () => {
    // 回归：服务端发完 done 帧即 close，客户端若仍重连会重复拉同一终态帧
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    renderHook(() => useJobStream('job-7'));
    act(() => {
      instances[0].onmessage?.({ data: JSON.stringify({ status: 'done', done: true }) });
      instances[0].onclose?.();
    });
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(Ctor).toHaveBeenCalledTimes(1);
  });

  it('非终态断连 → 有限重连（new 新 WebSocket）而非立即报错', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-4'));
    act(() => {
      instances[0].onmessage?.({ data: JSON.stringify({ progress: { done: 5, total: 10, phase: 'p' } }) });
      instances[0].onclose?.();
    });
    expect(result.current.done).toBe(false);
    act(() => {
      vi.advanceTimersByTime(1100);
    });
    expect(Ctor).toHaveBeenCalledTimes(2); // 第 1 次重连已发生
    // 新连接补发进度帧
    act(() => {
      instances[1].onmessage?.({ data: JSON.stringify({ progress: { done: 8, total: 10, phase: 'p' } }) });
    });
    expect(result.current.progress?.done).toBe(8);
  });

  it('重连 2 次仍失败 → 置错误终态防 busy 悬挂', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-5'));
    act(() => {
      instances[0].onclose?.();
      vi.advanceTimersByTime(1100);
      instances[1].onclose?.();
      vi.advanceTimersByTime(2200);
      instances[2].onclose?.();
    });
    expect(Ctor).toHaveBeenCalledTimes(3);
    expect(result.current.done).toBe(true);
    expect(result.current.error).toContain('任务连接中断');
  });

  it('卸载时不再重连、清理 timer', () => {
    const { Ctor, instances } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { unmount } = renderHook(() => useJobStream('job-6'));
    act(() => {
      instances[0].onclose?.();
    });
    unmount();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(Ctor).toHaveBeenCalledTimes(1);
  });
});
