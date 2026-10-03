'use client';

import { useEffect, useState } from 'react';

import type { RunTrace, TraceStep } from '@/lib/ask-stream';
import { summarizeArgs } from '@/lib/ask-stream';
import { fmtDuration } from '@/lib/format';

/**
 * 过程轨：本轮运行里 agent 想了什么、调了哪些工具、每个工具跑了多久、
 * 拿回了什么。**只覆盖当前这一轮**，是「活着的过程」，不是历史。
 *
 * 为什么不做历史重建：落库只存正文（`ask_messages`），过程数据
 * （thinking / 工具结果 / system）没有事实源 —— CLI 路径连工具名都不落库
 * （只有 mock provider 会把 `tool_calls` 写进消息）。硬「重建」只能是编的，
 * 所以刷新页面、切走再回来，过程轨就是空的。
 *
 * 运行中默认展开（这是「它在干活」的唯一可视证据），收尾后自动收起，
 * 用户手动展开/收起后不再被自动状态覆盖。
 */
export default function RunTraceView({
  trace,
  running,
  now,
}: {
  trace: RunTrace;
  running: boolean;
  /** 当前时刻（由父组件的心跳驱动，用于跑动中的耗时显示） */
  now: number;
}) {
  const [open, setOpen] = useState(running);
  const [touched, setTouched] = useState(false);

  // 新的一轮开始/结束：跟着运行状态走。用户手动拨过之后就不再抢方向盘。
  useEffect(() => {
    if (!touched) setOpen(running);
  }, [running, touched]);

  const steps = trace.steps;
  if (steps.length === 0 && !running) return null;

  const elapsed = (trace.finishedAt ?? now) - trace.startedAt;
  const tools = steps.filter((s) => s.kind === 'tool').length;
  const state = running ? '进行中' : trace.failed ? '已中断或失败' : '已完成';
  const dot = running ? 'bg-up animate-pulse' : trace.failed ? 'bg-down' : 'bg-ink-faint';

  return (
    <div className="border-b border-line bg-panel/60">
      <button
        type="button"
        onClick={() => {
          setTouched(true);
          setOpen((v) => !v);
        }}
        className="flex w-full items-center gap-2 px-4 py-1.5 text-xs text-ink-dim hover:text-up"
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="font-medium text-ink">{state}</span>
        <span className="text-ink-faint">
          {steps.length} 步{tools > 0 ? ` · 工具 ${tools} 次` : ''} · {fmtDuration(elapsed)}
        </span>
        <span className="ml-auto text-ink-faint">{open ? '收起 ▴' : '展开 ▾'}</span>
      </button>
      {open ? (
        <ol className="max-h-64 space-y-1 overflow-y-auto border-t border-line px-4 py-2">
          {steps.map((s) => (
            <TraceRow key={s.id} step={s} now={now} />
          ))}
          {steps.length === 0 ? (
            <li className="text-xs text-ink-faint">已发出请求，等待第一个事件…</li>
          ) : null}
        </ol>
      ) : null}
    </div>
  );
}

function TraceRow({ step, now }: { step: TraceStep; now: number }) {
  if (step.kind === 'note') {
    return (
      <li
        className={`text-xs ${step.level === 'warn' ? 'text-up' : 'text-ink-faint'}`}
      >
        · {step.text}
      </li>
    );
  }
  if (step.kind === 'thinking') {
    const ms = (step.finishedAt ?? now) - step.startedAt;
    return (
      <li className="border-l-2 border-line pl-2">
        <div className="text-xs text-ink-faint">
          推理{step.finishedAt === undefined ? '中' : ` · ${fmtDuration(ms)}`}
        </div>
        <div className="max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-ink-dim">
          {step.text}
        </div>
      </li>
    );
  }
  const ms = (step.finishedAt ?? now) - step.startedAt;
  const icon = step.status === 'running' ? '…' : step.status === 'error' ? '×' : '✓';
  const tone =
    step.status === 'running' ? 'text-up' : step.status === 'error' ? 'text-down' : 'text-ink-faint';
  return (
    <li className="border-l-2 border-line pl-2">
      <div className="flex items-baseline gap-2 text-xs">
        <span className={`w-3 shrink-0 text-center font-mono ${tone}`}>{icon}</span>
        <span className="font-mono text-ink">{step.name}</span>
        {step.status === 'running' ? (
          <span className="text-ink-faint">调用中…</span>
        ) : (
          <span className="text-ink-faint">{fmtDuration(ms)}</span>
        )}
      </div>
      {summarizeArgs(step.args) ? (
        <div className="ml-5 truncate font-mono text-[11px] text-ink-faint">
          {summarizeArgs(step.args)}
        </div>
      ) : null}
      {step.detail ? (
        <details className="ml-5 mt-0.5 text-[11px] text-ink-faint">
          <summary className="cursor-pointer select-none hover:text-up">
            结果{step.summary && step.summary !== step.detail ? `：${step.summary}` : ''}
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap border border-line bg-white p-1.5 font-mono">
            {step.detail}
          </pre>
        </details>
      ) : null}
    </li>
  );
}
