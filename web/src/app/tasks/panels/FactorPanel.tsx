'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import TaskTable from '../TaskTable';
import { fetcher, get, post } from '@/lib/api';
import type { TaskItem } from '../types';

type MineRun = {
  run_id: string;
  agent: string;
  generator: string;
  n_evaluated: number;
  n_static_fail: number;
  n_low_ic: number;
  n_redundant: number;
  n_size_proxy: number;
  n_survivors: number;
  created_at: string;
};

const GENERATORS = ['random', 'gp'];

/** 因子挖掘面板：发起表单 + 任务列表 + 挖掘台账 */
export default function FactorPanel() {
  const { data: tasks, isLoading, mutate } = useSWR<TaskItem[]>('/tasks?kind=factor', fetcher, {
    refreshInterval: (latest?: TaskItem[]) =>
      latest?.some((t) => t.state === 'running' || t.state === 'queued') ? 2_000 : 15_000,
  });
  const { data: runs, mutate: mutateRuns } = useSWR<MineRun[]>('/factors/mine/runs', get);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [agent, setAgent] = useState('gp-internal');
  const [generator, setGenerator] = useState('random');
  const [n, setN] = useState(100);

  async function launch() {
    setBusy('mine');
    setMsg('');
    if (!agent || !generator || !Number.isFinite(n) || n <= 0) {
      setMsg('✗ 请填写 Agent、生成器和正整数预算 n');
      setBusy('');
      return;
    }
    try {
      const r = await post<{ task_id: string; status: string }>('/factors/mine/run', {
        agent,
        generator,
        n,
      });
      setMsg(`✓ 挖掘任务已排队（${r.task_id.slice(0, 8)}…，${r.status}），进度见下方任务列表`);
      void mutate();
      void mutateRuns();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <Panel title="发起挖掘" meta="Agent × 生成器 × 预算 n · 202 排队">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-ink-faint">
            Agent
            <input
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              className="input input-mono mt-1 block w-40"
            />
          </label>
          <label className="text-xs text-ink-faint">
            生成器
            <select
              value={generator}
              onChange={(e) => setGenerator(e.target.value)}
              className="input mt-1 block"
            >
              {GENERATORS.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-ink-faint">
            预算 n
            <input
              type="number"
              value={n}
              min={10}
              max={5000}
              onChange={(e) => setN(Number(e.target.value))}
              className="input input-mono mt-1 block w-24"
            />
          </label>
          <button className="btn btn-primary" onClick={launch} disabled={busy !== ''}>
            {busy === 'mine' ? '发起中…' : '发起挖掘'}
          </button>
        </div>
        <Msg text={msg} />
      </Panel>

      <Panel title="挖掘任务" meta="队列任务 + 落账挖掘会话">
        <TaskTable
          tasks={tasks}
          loading={isLoading}
          msg=""
          extraOf={(t) => String(t.params?.generator ?? '—')}
          emptyHint="暂无挖掘任务 —— 上方发起一次"
        />
      </Panel>

      <Panel title="挖掘台账" meta="漏斗：评估 → G0 / LOW_IC / 冗余 / 风格代理 → 幸存">
        {runs && runs.length > 0 ? (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">run_id</th>
                <th className="text-left">Agent</th>
                <th className="text-left">生成器</th>
                <th className="text-right">评估</th>
                <th className="text-right">G0淘汰</th>
                <th className="text-right">LOW_IC</th>
                <th className="text-right">冗余</th>
                <th className="text-right">风格代理</th>
                <th className="text-right">幸存</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id} className="hover:bg-white">
                  <td className="font-mono">{r.run_id}</td>
                  <td className="text-xs">{r.agent}</td>
                  <td className="text-xs">{r.generator}</td>
                  <td className="text-right">{r.n_evaluated}</td>
                  <td className="text-right">{r.n_static_fail}</td>
                  <td className="text-right">{r.n_low_ic}</td>
                  <td className="text-right">{r.n_redundant}</td>
                  <td className="text-right">{r.n_size_proxy}</td>
                  <td className="text-right font-medium">{r.n_survivors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有挖掘会话 —— 上方发起一次</Empty>
        )}
      </Panel>
    </div>
  );
}
