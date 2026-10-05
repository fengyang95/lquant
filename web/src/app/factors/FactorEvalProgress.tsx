'use client';

import ProgressBar from '@/components/ProgressBar';
import type { JobProgress } from '@/lib/streaming';

/** 评价任务进度行：进度条 + 任务号 + 取消 + 任务中心入口。
 *
 *  四处评价入口此前各写一份（编辑器甚至只有一行纯文字、既无进度也无法取消），
 *  这里统一成同一块 UI，配合 `useFactorEval` 一起复用。 */
export default function FactorEvalProgress({
  jobId,
  progress,
  onCancel,
}: {
  jobId: string;
  progress: JobProgress | null;
  onCancel?: () => void | Promise<void>;
}) {
  return (
    <div className="mt-3 w-72 space-y-1">
      {progress && progress.total > 0 ? (
        <ProgressBar
          pct={(progress.done / progress.total) * 100}
          phase={progress.phase}
        />
      ) : null}
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-faint">
        <span>评价任务 {jobId} 运行中…</span>
        {onCancel ? (
          <button type="button" className="btn btn-sm" onClick={() => void onCancel()}>
            取消
          </button>
        ) : null}
        <a className="underline" href="/tasks">
          任务管理
        </a>
      </div>
    </div>
  );
}
