'use client';

/** 确定性进度条：0-100。流式更新由 useJobStream 驱动（transition 平滑流转）。 */
export default function ProgressBar({
  pct,
  phase,
}: {
  pct: number;
  phase?: string | null;
}) {
  const clamped = Math.max(0, Math.min(100, Math.round(pct)));
  return (
    <div className="min-w-32 flex-1">
      <div className="h-1.5 w-full bg-paper">
        <div
          className="h-1.5 bg-indigo transition-all duration-500 ease-out"
          style={{ width: `${clamped}%` }}
        />
      </div>
      <div className="mt-0.5 flex justify-between text-[11px] tabular-nums text-ink-faint">
        <span className="max-w-48 truncate" title={phase ?? undefined}>{phase || ''}</span>
        <span>{clamped}%</span>
      </div>
    </div>
  );
}
