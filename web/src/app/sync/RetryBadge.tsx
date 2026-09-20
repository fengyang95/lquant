'use client';

/** 「重试中」徽标：作业行 / 摘要卡共用。非重试状态渲染 null。 */
export default function RetryBadge({
  retryCount,
  nextRetryAt,
}: {
  retryCount: number | null | undefined;
  nextRetryAt: string | null | undefined;
}) {
  const rc = Number(retryCount ?? 0);
  const hasNext = nextRetryAt != null && nextRetryAt !== '';
  if (!(rc > 0 || hasNext)) return null;
  const attempt = Math.max(rc, 1);
  const at = nextRetryAt ? String(nextRetryAt).slice(11, 16) : null;
  return (
    <span
      data-testid="retry-badge"
      className="ml-2 inline-block rounded-[2px] border border-gold/40 bg-gold/10 px-1.5 py-0.5 text-xs text-gold"
    >
      {at ? `重试中（第 ${attempt} 次，${at} 重试）` : `重试中（第 ${attempt} 次）`}
    </span>
  );
}
