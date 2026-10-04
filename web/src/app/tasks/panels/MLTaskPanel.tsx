'use client';

/** ML 任务面板：训练 / 滚动重训列表 + 协作式取消（走 /tasks/ml/{id}/cancel）。 */
import useSWR from 'swr';
import { useState } from 'react';
import { Panel } from '@/components/Panel';
import TaskTable from '../TaskTable';
import { fetcher, post } from '@/lib/api';
import type { TaskItem } from '../types';

export default function MLTaskPanel() {
  const { data: tasks, isLoading, mutate } = useSWR<TaskItem[]>('/tasks?kind=ml', fetcher, {
    refreshInterval: (latest?: TaskItem[]) =>
      latest?.some((t) => t.state === 'running' || t.state === 'queued') ? 2_000 : 15_000,
  });
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');

  async function cancelTask(id: string) {
    setBusy('cancel');
    setMsg('');
    try {
      await post(`/tasks/ml/${encodeURIComponent(id)}/cancel`, {});
      setMsg('✓ 已请求取消');
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <Panel title="ML 任务" meta="训练 / 滚动重训 · lquant-ml 队列">
        <TaskTable
          tasks={tasks ?? []}
          loading={!!isLoading}
          msg={msg}
          emptyHint="暂无 ML 任务。到「模型」页发起训练或滚动重训。"
          actionsOf={(t) =>
            t.state === 'running' || t.state === 'queued' ? (
              <button className="btn text-xs" disabled={busy === 'cancel'}
                onClick={() => cancelTask(t.id)}>取消</button>
            ) : null
          }
        />
      </Panel>
    </div>
  );
}
