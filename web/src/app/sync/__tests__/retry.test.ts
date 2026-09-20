import { describe, expect, it } from 'vitest';
import { retryState, retryBadgeText, runRetryText } from '../retry';

describe('retryState', () => {
  it('retry_count>0 或 next_retry_at 非空 → retrying', () => {
    expect(retryState(0, null)).toEqual({ retrying: false, attempt: 0, next: null });
    expect(retryState(2, null).retrying).toBe(true);
    expect(retryState(0, '2026-09-18T16:05').retrying).toBe(true);
    expect(retryState(null, null).retrying).toBe(false);
    expect(retryState(0, '').retrying).toBe(false);
  });
});

describe('retryBadgeText', () => {
  it('重试中徽标：含轮次与 HH:MM；未重试为 null', () => {
    expect(retryBadgeText(0, null)).toBeNull();
    expect(retryBadgeText(2, '2026-09-18T16:05:00')).toBe('重试中（第 2 次，16:05 重试）');
    expect(retryBadgeText(1, null)).toBe('重试中（第 1 次）');
    expect(retryBadgeText(null, '2026-09-18T16:05:00')).toBe('重试中（第 1 次，16:05 重试）');
  });
});

describe('runRetryText', () => {
  it('历史 detail 的轮次 / 下次重试文案', () => {
    expect(runRetryText(null)).toBeNull();
    expect(runRetryText({})).toBeNull();
    expect(runRetryText({ attempt: 1 })).toBeNull();
    expect(runRetryText({ attempt: 2 })).toBe('第 2 轮');
    expect(runRetryText({ next_retry_at: '2026-09-18T16:05:00' })).toBe('下次重试 16:05');
    expect(runRetryText({ attempt: 3, next_retry_at: '2026-09-18T16:05:00' })).toBe('第 3 轮 · 下次重试 16:05');
  });
});
