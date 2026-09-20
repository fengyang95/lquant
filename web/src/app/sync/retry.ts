/** 重试可见性纯函数 —— 供 /sync 页与测试共用 */

export type RetryState = {
  retrying: boolean;
  /** 已尝试轮次（初始跑=1，重试=2/3…） */
  attempt: number;
  /** 下次重试时刻（ISO 字符串，未排程为 null） */
  next: string | null;
};

/** retry_count>0 或 next_retry_at 非空 → 处于重试链路 */
export function retryState(retryCount: number | null | undefined, nextRetryAt: string | null | undefined): RetryState {
  const rc = Number(retryCount ?? 0);
  const hasNext = nextRetryAt != null && nextRetryAt !== '';
  const attempt = rc > 0 ? rc : hasNext ? 1 : 0;
  return {
    retrying: rc > 0 || hasNext,
    attempt,
    next: hasNext ? String(nextRetryAt) : null,
  };
}

/** 作业行徽标文案：`重试中（第 N 次，HH:MM 重试）`；不在重试返回 null */
export function retryBadgeText(retryCount: number | null | undefined, nextRetryAt: string | null | undefined): string | null {
  if (!retryState(retryCount, nextRetryAt).retrying) return null;
  const attempt = Math.max(Number(retryCount ?? 0), 1);
  const at = nextRetryAt ? nextRetryAt.slice(11, 16) : null;
  return at
    ? `重试中（第 ${attempt} 次，${at} 重试）`
    : `重试中（第 ${attempt} 次）`;
}

/** 历史行 detail 中的尝试轮次 / 下次重试文案；无重试信息返回 null */
export function runRetryText(detail: Record<string, unknown> | null | undefined): string | null {
  if (!detail || typeof detail !== 'object') return null;
  const attempt = detail.attempt;
  const next = detail.next_retry_at;
  const hasAttempt = typeof attempt === 'number' && attempt > 1;
  if (!hasAttempt && next == null) return null;
  const parts: string[] = [];
  if (hasAttempt) parts.push(`第 ${attempt} 轮`);
  if (typeof next === 'string' && next) parts.push(`下次重试 ${next.slice(11, 16)}`);
  return parts.length ? parts.join(' · ') : null;
}
