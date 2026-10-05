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
 *  jobId 为 null 时不连接（IDLE 态）。页面刷新后重连可拿到终态（job 仍在注册表）。
 *
 *  `reconnectKey` 变化会强制重新订阅并清空上一轮状态：任务 id 可能是确定性的
 *  （因子评价同一因子重跑 → 同一个 `factor-eval-{factor}`），只比较 jobId 会把
 *  界面停在上一轮结果上。调用方每次重跑自增该值即可，不必依赖中间渲染。 */
export function useJobStream<T = unknown>(
  jobId: string | null,
  reconnectKey: number = 0,
): JobStream<T> {
  const [state, setState] = useState<JobStream<T>>(IDLE as JobStream<T>);

  useEffect(() => {
    if (!jobId) {
      setState(IDLE as JobStream<T>);
      return;
    }
    // 新订阅先清空上一轮终态（IDLE 是模块常量，同引用时 React 会跳过重渲染）
    setState(IDLE as JobStream<T>);
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(jobId)}`;
    let alive = true;
    let closed = false;
    let retry = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let ws: WebSocket | null = null;

    const handleMsg = (raw: string): void => {
      try {
        const m = JSON.parse(raw) as {
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
          error: m.error
            ?? (m.status === 'interrupted' ? '任务因服务重启已中断，请重新发起' : undefined)
            ?? prev.error,
          done: Boolean(m.done),
        }));
      } catch {
        /* 非 JSON 帧忽略 */
      }
    };

    const connect = (): void => {
      if (!alive || closed) return;
      ws = new WebSocket(url);
      ws.onmessage = (ev: MessageEvent<string>) => handleMsg(ev.data);
      ws.onclose = () => {
        if (closed || !alive) return;
        // 服务端未发终态就断连：有限重连（网络抖动/代理超时下任务仍在跑），
        // 重试 2 次仍失败才置错误终态，防 busy 悬挂
        if (retry < 2) {
          retry += 1;
          retryTimer = setTimeout(connect, 1000 * retry);
          return;
        }
        closed = true;
        alive = false;
        setState((prev) => prev.done
          ? prev
          : { ...prev, error: prev.error ?? '任务连接中断，请刷新重试', done: true });
      };
    };

    connect();

    return () => {
      alive = false;
      if (retryTimer !== null) clearTimeout(retryTimer);
      ws?.close();
    };
  }, [jobId, reconnectKey]);

  return state;
}
