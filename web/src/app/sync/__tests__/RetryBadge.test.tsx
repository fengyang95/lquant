import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import RetryBadge from '../RetryBadge';

describe('RetryBadge', () => {
  it('retry_count>0 → 显示重试徽标', () => {
    render(<RetryBadge retryCount={2} nextRetryAt={null} />);
    const badge = screen.getByTestId('retry-badge');
    expect(badge.textContent).toBe('重试中（第 2 次）');
  });

  it('next_retry_at 非空 → 显示含 HH:MM 的徽标', () => {
    render(<RetryBadge retryCount={0} nextRetryAt="2026-09-18T16:05:00" />);
    expect(screen.getByTestId('retry-badge').textContent).toBe('重试中（第 1 次，16:05 重试）');
  });

  it('无重试信息 → 不渲染', () => {
    render(<RetryBadge retryCount={0} nextRetryAt={null} />);
    expect(screen.queryByTestId('retry-badge')).toBeNull();
  });
});
