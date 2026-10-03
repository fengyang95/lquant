'use client';

import { useState } from 'react';
import useSWR from 'swr';

import { PROVIDER_LABELS } from '@/lib/agent-api';
import { killRun, listRuns } from '@/lib/ask-api';
import { fmtDuration } from '@/lib/format';

/** 轮询间隔：比「已运行」的显示精度（1s）略宽，够看又不会把接口刷爆。 */
const POLL_MS = 3000;

/**
 * 「运行中」指示器：谁在跑、跑了多久、pid 多少，并能直接终止。
 *
 * 为什么要做成页面级而不是只放在会话里：用户发现「一直在转圈」时，往往已经
 * 切走或开了好几个会话 —— 只看当前会话根本不知道是谁在占资源。pid 是有意
 * 露出来的：能拿它去 ps 一眼，比只看到一个「已运行 300 秒」有用得多
 * （详见 ``GET /ask/runs``）。
 *
 * 并发上限也在这里显示：被 429 拒掉时，用户第一反应是「坏了」，看到
 * 「上限 4」才知道是撞了闸。
 */
export default function RunningRuns({ onOpen }: { onOpen?: (sid: string) => void }) {
  const { data, mutate } = useSWR('/ask/runs', listRuns, { refreshInterval: POLL_MS });
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const runs = data?.runs ?? [];

  const kill = async (sid: string) => {
    setBusy(sid);
    setErr('');
    try {
      await killRun(sid);
      // 不等下一轮轮询：点完立刻刷新，否则按钮还要“装作”忙 3 秒
      await mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => setOpen((v) => !v)}
        title="正在跑的 agent 回答"
      >
        {runs.length > 0 ? `● 运行中 ${runs.length}` : '○ 运行中 0'}
      </button>

      {open ? (
        <div
          className="absolute right-0 z-40 mt-1 w-[420px] border border-line-strong bg-paper p-3 shadow-lg"
          role="dialog"
          aria-label="运行中的 agent"
        >
          <div className="mb-2 flex items-baseline justify-between">
            <span className="text-xs font-medium text-ink">正在跑的 agent</span>
            <span className="text-[11px] text-ink-faint">
              上限 {data?.max_concurrent_runs ?? '—'} 个（超出会被直接拒绝，不排队）
            </span>
          </div>

          {err ? <div className="mb-2 text-xs text-up">{err}</div> : null}

          {runs.length === 0 ? (
            <div className="py-4 text-center text-xs text-ink-faint">当前没有在跑的回答</div>
          ) : (
            <ul className="divide-y divide-line">
              {runs.map((r) => (
                <li key={r.session_id} className="flex items-center gap-2 py-1.5 text-xs">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-ink">
                      {PROVIDER_LABELS[r.provider] ?? r.provider}
                      <span className="ml-2 text-ink-faint">
                        已运行 {fmtDuration(r.elapsed_seconds * 1000)}
                      </span>
                    </div>
                    <div className="truncate font-mono text-[11px] text-ink-faint">
                      {r.pid ? `pid ${r.pid}` : '无子进程'}
                      {r.workspace ? ` · ${r.workspace}` : ''}
                    </div>
                  </div>
                  {onOpen ? (
                    <button
                      type="button"
                      className="btn btn-sm shrink-0"
                      onClick={() => {
                        onOpen(r.session_id);
                        setOpen(false);
                      }}
                    >
                      打开
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="btn btn-sm shrink-0 text-up"
                    disabled={busy === r.session_id}
                    onClick={() => void kill(r.session_id)}
                  >
                    {busy === r.session_id ? '终止中…' : '终止'}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
