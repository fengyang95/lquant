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

/** 是否因子评价任务。优先结构化 subtype（后端按 job_results 判定），
 *  回退显示名 —— 名称是自由文本，只作老数据/老后端的兜底。 */
function isEvalTask(t: TaskItem): boolean {
  return t.subtype === 'factor_eval' || t.name === '因子评价';
}

/** 因子挖掘面板：发起表单 + 任务列表（评价 + 挖掘）+ 挖掘台账 */
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

  /** 运行中任务协作式取消 */
  async function cancelTask(id: string) {
    setBusy('cancel');
    setMsg('');
    try {
      await post(`/tasks/factor/${encodeURIComponent(id)}/cancel`, {});
      setMsg('✓ 已请求取消');
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  /** 已完成的「因子评价」任务 → 取落库结果，跳转评价报告 */
  async function openResult(t: TaskItem) {
    setBusy(`res-${t.id}`);
    setMsg('');
    try {
      const r = await get<{ result: { report_url?: string } }>(
        `/factors/evaluate/${encodeURIComponent(t.id)}`,
      );
      if (r.result?.report_url) window.open(r.result.report_url, '_blank');
      else setMsg('✗ 结果中缺少报告地址');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  /** 详情列：评价显示「在评哪个因子」（可跳因子详情），挖掘显示 Agent / 生成器。
   *  后端已把 params 回填（task_center._factor_items），此前恒为 `—`。 */
  function detailOf(t: TaskItem) {
    if (isEvalTask(t)) {
      const factor = typeof t.params?.factor === 'string' ? t.params.factor : '';
      if (!factor) return '—';
      return (
        <a className="underline" href={`/factors/${encodeURIComponent(factor)}`}>
          {factor}
        </a>
      );
    }
    const agent = t.params?.agent;
    const gen = t.params?.generator;
    if (!agent && !gen) return '—';
    return `${agent ?? '—'} / ${gen ?? '—'}`;
  }

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

      <Panel title="挖掘 / 评价任务" meta="同一条 lquant-mining 队列 · 详情列显示评价因子或挖掘 Agent">
        <TaskTable
          tasks={tasks}
          loading={isLoading}
          msg=""
          extraOf={detailOf}
          actionsOf={(t) => (
            <>
              {isEvalTask(t) && t.state === 'finished' && (
                <button
                  className="btn btn-sm"
                  disabled={busy === `res-${t.id}`}
                  onClick={() => void openResult(t)}
                >
                  查看结果
                </button>
                )}
              {/* queued 也要能取消：后端 request_cancel 支持排队取消（RQ job.cancel），
                  此前 UI 只在 running 显示，把最该取消的排队任务挡在外面 */}
              {(t.state === 'running' || t.state === 'queued') && (
                <button
                  className="btn btn-sm"
                  disabled={busy === 'cancel'}
                  onClick={() => void cancelTask(t.id)}
                >
                  取消
                </button>
              )}
            </>
          )}
          emptyHint="暂无挖掘 / 评价任务 —— 上方发起一次，或去「因子」页运行评价"
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
