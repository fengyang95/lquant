'use client';

import { useState } from 'react';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Stat } from '@/components/Panel';
import { fetcher } from '@/lib/api';
import { Empty } from '@/components/States';
import DataPanel from './panels/DataPanel';
import SyncPanel from './panels/SyncPanel';
import BacktestPanel from './panels/BacktestPanel';
import FactorPanel from './panels/FactorPanel';
import { KIND_TEXT } from './types';
import type { TaskSummary, TaskItem } from './types';

const TABS: TaskItem['kind'][] = ['data', 'sync', 'backtest', 'factor'];

const TAB_LABEL: Record<TaskItem['kind'], string> = {
  data: '数据',
  sync: '同步',
  backtest: '回测',
  factor: '因子挖掘',
};

/** summary 数字 → 语气色：失败红、运行中蓝 */
function toneOf(s: { running: number; failed: number }): string {
  if (s.failed > 0) return 'text-up';
  if (s.running > 0) return 'text-indigo';
  return 'text-ink';
}
/** summary 数字 → 语气色辅助（卡片上显示的次要指标） */
function subText(s: { total: number; failed: number; succeeded: number }): string {
  return `共 ${s.total} · 败 ${s.failed}`;
}

export default function TasksPage() {
  const { data: summary } = useSWR<TaskSummary>('/tasks/summary', fetcher, {
    refreshInterval: 10_000,
  });
  const [tab, setTab] = useState<TaskItem['kind']>('data');

  return (
    <div className="space-y-5">
      <PageHeader title="任务管理" sub="四类后台任务统一入口 · 顶卡为全局状态，面板内各自轮询" />
      {summary ? (
        <div className="grid grid-cols-2 gap-4 border border-line bg-panel p-4 lg:grid-cols-4">
          {TABS.map((k) => {
            const s = summary.kinds[k];
            if (!s) return null;
            return (
              <button key={k} onClick={() => setTab(k)} className="min-w-0 text-left">
                <Stat label={KIND_TEXT[k]} value={s.running} tone={toneOf(s)} hint={subText(s)} />
              </button>
            );
          })}
        </div>
      ) : (
        <Empty>加载中…</Empty>
      )}
      <div className="flex gap-1 border-b border-line">
        {TABS.map((k) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm ${
              tab === k
                ? 'border-ink font-medium text-ink'
                : 'border-transparent text-ink-dim hover:text-ink'
            }`}
          >
            {TAB_LABEL[k]}
          </button>
        ))}
      </div>
      <div>
        {tab === 'data' && <DataPanel />}
        {tab === 'sync' && <SyncPanel />}
        {tab === 'backtest' && <BacktestPanel />}
        {tab === 'factor' && <FactorPanel />}
      </div>
    </div>
  );
}
