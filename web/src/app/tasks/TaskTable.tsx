'use client';

import { Empty, Msg } from '@/components/States';
import { STATE_BADGE, STATE_TEXT, createdText, paramsBrief } from './types';
import type { TaskItem } from './types';

/** 统一状态徽章（复用 TaskStatusBadge 的方角描边语义，running 带脉冲点） */
export function StateBadge({ state }: { state: TaskItem['state'] }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-[2px] border px-1.5 py-0.5 text-xs ${
        STATE_BADGE[state] ?? STATE_BADGE.queued
      }`}
    >
      {STATE_TEXT[state] ?? state}
      {state === 'running' && <span className="ml-1 animate-pulse">●</span>}
    </span>
  );
}

/** 统一任务表：徽章 / 名称 / 时间 / 参数摘要 / 操作列，extraOf 可选详情列。
 *  操作按钮组由各 Panel 通过 actionsOf 注入（重试 / 取消 / 详情等）。 */
export default function TaskTable({
  tasks,
  loading,
  msg,
  actionsOf,
  extraOf,
  emptyHint,
}: {
  tasks: TaskItem[] | undefined;
  loading: boolean;
  msg: string;
  actionsOf?: (t: TaskItem) => React.ReactNode;
  extraOf?: (t: TaskItem) => React.ReactNode;
  emptyHint: string;
}) {
  const cols = 5 + (extraOf ? 1 : 0);
  return (
    <div className="space-y-3">
      <Msg text={msg} />
      {loading && !tasks ? (
        <div className="py-8 text-center text-xs text-ink-faint">加载中…</div>
      ) : !tasks?.length ? (
        <Empty>{emptyHint}</Empty>
      ) : (
        <table className="table-dense">
          <thead>
            <tr>
              <th className="text-left">状态</th>
              <th className="text-left">名称</th>
              <th className="text-left">参数</th>
              {extraOf ? <th className="text-left">详情</th> : null}
              <th className="text-left">创建时间</th>
              <th className="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id} className="hover:bg-white">
                <td>
                  <StateBadge state={t.state} />
                  {t.error && (
                    <div className="max-w-52 truncate text-xs text-up" title={t.error}>
                      {t.error}
                    </div>
                  )}
                </td>
                <td>
                  <div className="font-medium">{t.name}</div>
                  <div className="font-mono text-xs text-ink-faint">{t.id.slice(0, 8)}</div>
                </td>
                <td className="max-w-64 truncate font-mono text-xs text-ink-dim" title={JSON.stringify(t.params)}>
                  {paramsBrief(t.params) || '—'}
                </td>
                {extraOf ? <td className="text-xs text-ink-dim">{extraOf(t)}</td> : null}
                <td className="whitespace-nowrap text-xs tabular-nums text-ink-dim">
                  {createdText(t.created_at)}
                </td>
                <td className="whitespace-nowrap text-right">{actionsOf ? actionsOf(t) : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
