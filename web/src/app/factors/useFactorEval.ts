'use client';

/**
 * 因子评价的统一发起 / 跟踪 hook。
 *
 * 四处入口（因子库快速评价、因子详情、编辑器「保存并评价」、任务中心）此前各写
 * 一套「POST /factors/evaluate → 盯进度 → 收结果」，编辑器还用另一套 REST 轮询且
 * 读错了响应契约。这里收敛成一条链路：
 *
 *   POST /factors/evaluate  →  202 {job_id}  →  WS /ws/jobs/{job_id}
 *
 * 进度、终态 result、失败 error 全部来自 WS 帧（协议见 server/ws.py）；取消复用
 * 任务中心已有的 POST /tasks/factor/{id}/cancel。
 */
import { useCallback, useState } from 'react';

import { post } from '@/lib/api';
import { useJobStream, type JobProgress } from '@/lib/streaming';

/** 评价请求体：与后端 `EvaluateIn` 对应；不传的字段走服务端默认。 */
export type FactorEvalParams = {
  /** 报告名：仅 `[A-Za-z0-9_-]`（后端做路径穿越校验） */
  factor: string;
  formula: string;
  n_groups?: number;
  start?: string;
  end?: string | null;
  universe?: string;
  filter_zscore?: number | null;
  event_window?: number[];
  steps?: Record<string, unknown>[] | null;
  with_robustness?: boolean;
};

export type FactorEvalRun<T> = {
  /** 当前任务 id（确定性：`factor-eval-{factor}`），未发起时为 null */
  jobId: string | null;
  /** 入队请求进行中 */
  starting: boolean;
  /** 入队失败（HTTP 层）；与「任务跑失败」区分开 */
  startError: string;
  progress: JobProgress | null;
  status: string | null;
  done: boolean;
  result: T | null;
  /** 入队错误、任务错误、无 result 的异常终态，归并成一句可直接展示的话 */
  failure: string | null;
  /** 已入队、未出结果、未失败 */
  running: boolean;
  start: (params: FactorEvalParams) => Promise<string | null>;
  cancel: () => Promise<void>;
  reset: () => void;
};

export function useFactorEval<T = unknown>(): FactorEvalRun<T> {
  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState('');
  // 重连序号：评价 job id 是确定性的（同因子重跑同一个 id），只比较 jobId
  // 不会触发重订阅，界面会停在上一轮结果上；每次 start 自增强制重连。
  const [runSeq, setRunSeq] = useState(0);
  const stream = useJobStream<T>(jobId, runSeq);

  const start = useCallback(async (params: FactorEvalParams) => {
    setStarting(true);
    setStartError('');
    try {
      const r = await post<{ job_id: string }>('/factors/evaluate', params);
      setRunSeq((s) => s + 1);
      setJobId(r.job_id);
      return r.job_id;
    } catch (e: unknown) {
      setStartError(e instanceof Error ? e.message : String(e));
      // 入队失败即断开：否则会继续订阅上一轮（同 id）的任务
      setJobId(null);
      return null;
    } finally {
      setStarting(false);
    }
  }, []);

  const cancel = useCallback(async () => {
    if (!jobId) return;
    try {
      await post(`/tasks/factor/${encodeURIComponent(jobId)}/cancel`, {});
    } catch {
      // 已结束 / 不可取消（409）：状态流会给出真实终态，不在这里造第二套错误
    }
  }, [jobId]);

  const reset = useCallback(() => {
    setJobId(null);
    setStartError('');
  }, []);

  // 无 result 的终态（not_found / canceled / 任务体抛错但 WS 未带 error）
  const streamFailure = stream.done && !stream.result
    ? `评价任务异常结束（${stream.status ?? 'unknown'}）`
    : null;
  const failure = startError || stream.error || streamFailure;
  const running = jobId !== null && !stream.done && !stream.result && !failure;

  return {
    jobId,
    starting,
    startError,
    progress: stream.progress,
    status: stream.status,
    done: stream.done,
    result: stream.result,
    failure,
    running,
    start,
    cancel,
    reset,
  };
}
