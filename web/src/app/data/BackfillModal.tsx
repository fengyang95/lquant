'use client';

import { useState } from 'react';
import { post } from '@/lib/api';

/** 全量回填弹窗：起止日期（默认 2016-01-01 ~ 今天）→ POST /data/tasks */
export default function BackfillModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (taskId: string) => void;
}) {
  const today = new Date().toISOString().slice(0, 10);
  const [start, setStart] = useState('2016-01-01');
  const [end, setEnd] = useState(today);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function submit() {
    if (!start || !end) {
      setErr('请填写起止日期');
      return;
    }
    if (start > end) {
      setErr('开始日期不能晚于结束日期');
      return;
    }
    setBusy(true);
    setErr('');
    try {
      const r = await post<{ task_id: string }>('/data/tasks', {
        kind: 'full_backfill',
        params: { start, end },
      });
      onCreated(r.task_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md border border-line bg-paper p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-1 font-song text-lg font-semibold">全量回填</h3>
        <p className="mb-4 text-xs text-ink-faint">
          按时间范围补齐日线湖（主源按 providers_order 依次尝试）。创建后后台执行，可关闭弹窗在任务列表看进度。
        </p>
        <div className="mb-3 grid grid-cols-2 gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-faint">开始日期</span>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="input input-mono w-full"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-faint">结束日期</span>
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="input input-mono w-full"
            />
          </label>
        </div>
        {err && <p className="mb-3 border-l-2 border-up bg-panel px-3 py-2 text-sm text-up">{err}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? '创建中…' : '创建任务'}
          </button>
        </div>
      </div>
    </div>
  );
}
