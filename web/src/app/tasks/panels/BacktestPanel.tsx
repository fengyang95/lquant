'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import TaskTable from '../TaskTable';
import { fetcher, post } from '@/lib/api';
import type { TaskItem } from '../types';

const REBALANCE_OPTIONS = ['daily', 'weekly', 'monthly'];

/** 回测任务面板：sweep 列表（/api/tasks?kind=backtest）+ 发起 sweep + 取消 */
export default function BacktestPanel() {
  const { data: tasks, isLoading, mutate } = useSWR<TaskItem[]>('/tasks?kind=backtest', fetcher, {
    refreshInterval: (latest?: TaskItem[]) =>
      latest?.some((t) => t.state === 'running' || t.state === 'queued') ? 2_000 : 15_000,
  });
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [formula, setFormula] = useState('pct_change_20');
  const [param, setParam] = useState('');
  const [paramValues, setParamValues] = useState('');
  const [rebalance, setRebalance] = useState('monthly');

  async function launch() {
    setBusy('sweep');
    setMsg('');
    const values = paramValues
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!formula || !param || values.length === 0) {
      setMsg('✗ 请填写公式、参数名和至少一个参数值');
      setBusy('');
      return;
    }
    try {
      await post('/backtests/sweep', {
        formula,
        param,
        values,
        rebalance,
      });
      setMsg(`✓ sweep 已发起：${param} ∈ {${values.join(', ')}}，稍后在列表看进度`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function cancel(jobId: string) {
    setBusy(jobId);
    setMsg('');
    try {
      await post(`/tasks/backtest/${jobId}/cancel`, {});
      setMsg('✓ 已请求取消');
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  function actionsOf(t: TaskItem) {
    const active = t.state === 'running' || t.state === 'queued';
    if (!active) return null;
    return (
      <button
        className="btn btn-sm border-up/40 text-up hover:bg-paper"
        onClick={() => cancel(t.id)}
        disabled={busy !== ''}
      >
        {busy === t.id ? '取消中…' : '取消'}
      </button>
    );
  }

  return (
    <div className="space-y-5">
      <Panel title="发起 Sweep" meta="单参数网格 · POST /api/backtests/sweep">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-ink-faint">
            公式
            <input
              value={formula}
              onChange={(e) => setFormula(e.target.value)}
              className="input input-mono mt-1 block w-44"
            />
          </label>
          <label className="text-xs text-ink-faint">
            参数名
            <input
              value={param}
              onChange={(e) => setParam(e.target.value)}
              className="input input-mono mt-1 block w-28"
            />
          </label>
          <label className="text-xs text-ink-faint">
            参数值（逗号分隔）
            <input
              value={paramValues}
              onChange={(e) => setParamValues(e.target.value)}
              className="input input-mono mt-1 block w-52"
              placeholder="5, 10, 20, 60"
            />
          </label>
          <label className="text-xs text-ink-faint">
            调仓
            <select value={rebalance} onChange={(e) => setRebalance(e.target.value)} className="input mt-1 block">
              {REBALANCE_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>
          <button className="btn btn-primary" onClick={launch} disabled={busy !== ''}>
            {busy === 'sweep' ? '发起中…' : '发起 Sweep'}
          </button>
        </div>
      </Panel>

      <Panel title="回测任务" meta="排队 / 运行 / 落账的任务">
        <TaskTable
          tasks={tasks}
          loading={isLoading}
          msg={msg}
          actionsOf={actionsOf}
          extraOf={(t) => String(t.params?.formula ?? t.params?.name ?? '—')}
          emptyHint="暂无回测任务 —— 上方发起一次 sweep"
        />
      </Panel>
    </div>
  );
}
