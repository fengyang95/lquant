import { renderHook, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useJobStream } from '../streaming';

function makeWsMock() {
  const inst = {
    onmessage: null as ((ev: { data: string }) => void) | null,
    onclose: null as (() => void) | null,
    close: vi.fn(),
  };
  const Ctor = vi.fn().mockImplementation(() => inst);
  return { Ctor, inst };
}

describe('useJobStream interrupted frame', () => {
  it('interrupted 终态帧 → error 提示重新发起，done=true', () => {
    const { Ctor, inst } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-1'));
    act(() => {
      inst.onmessage?.({ data: JSON.stringify({ status: 'interrupted', done: true }) });
    });
    expect(result.current.done).toBe(true);
    expect(result.current.error).toContain('已中断');
    vi.unstubAllGlobals();
  });

  it('显式 error 帧优先于 interrupted 兜底文案', () => {
    const { Ctor, inst } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-2'));
    act(() => {
      inst.onmessage?.({ data: JSON.stringify({ status: 'failed', error: '行情源不可用', done: true }) });
    });
    expect(result.current.error).toBe('行情源不可用');
    vi.unstubAllGlobals();
  });

  it('终态后 onclose 不覆盖既有状态', () => {
    const { Ctor, inst } = makeWsMock();
    vi.stubGlobal('WebSocket', Ctor);
    const { result } = renderHook(() => useJobStream('job-3'));
    act(() => {
      inst.onmessage?.({ data: JSON.stringify({ status: 'interrupted', done: true }) });
      inst.onclose?.();
    });
    expect(result.current.error).toContain('已中断');
    expect(result.current.done).toBe(true);
    vi.unstubAllGlobals();
  });
});
