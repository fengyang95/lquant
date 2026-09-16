'use client';

import { useEffect, useState } from 'react';

/** 任务进度（与后端 progress 注册表协议一致） */
export type JobProgress = {
  done: number;
  total: number;
  phase: string;
  message?: string | null;
};

export type JobStream<T = unknown> = {
  status: string | null;
  progress: JobProgress | null;
  result: T | null;
  error: string | null;
  done: boolean;
};

const IDLE: JobStream<never> = {
  status: null,
  progress: null,
  result: null,
  error: null,
  done: false,
};

/** WS /ws/jobs/{id} 流式订阅：进度随流下发，终态带 result / error 后服务端关连。
 *  jobId 为 null 时不连接（IDLE 态）。页面刷新后重连可拿到终态（job 仍在注册表）。 */
export function useJobStream<T = unknown>(jobId: string | null): JobStream<T> {
  const [state, setState] = useState<JobStream<T>>(IDLE as JobStream<T>);

  useEffect(() => {
    if (!jobId) {
      setState(IDLE as JobStream<T>);
      return;
    }
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(jobId)}`;
    let alive = true;
    const ws = new WebSocket(url);
    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const m = JSON.parse(ev.data) as {
          status?: string;
          progress?: JobProgress;
          result?: T;
          error?: string;
          done?: boolean;
        };
        if (!alive) return;
        // 帧合并而非整体替换：终态帧不带 progress，整体替换会让进度条瞬间清空
        setState((prev) => ({
          status: m.status ?? prev.status,
          progress: m.progress ?? prev.progress,
          result: m.result ?? prev.result,
          error: m.error ?? prev.error,
          done: Boolean(m.done),
        }));
      } catch {
        /* 非 JSON 帧忽略 */
      }
    };
    ws.onclose = () => {
      alive = false;
      // 服务端未发终态就断连（API 重启 / 网络抖动）：置终态错误，防 busy 悬挂
      setState((prev) => prev.done
        ? prev
        : { ...prev, error: prev.error ?? '任务连接中断，请刷新重试', done: true });
    };
    return () => {
      alive = false;
      ws.close();
    };
  }, [jobId]);

  return state;
}
