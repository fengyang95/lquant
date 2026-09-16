'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';

type GapsDataset = {
  dataset: string;
  label: string;
  expected_days: number;
  actual_days: number;
  missing: string[];
  sparse_total: number;
};

type GapsResp = {
  window: { start: string; end: string };
  datasets: GapsDataset[];
};

/** 数据缺口面板：每个数据集一行 —— 应有交易日 vs 实际有数据，
 *  缺口日列表 + 一键补采（建 daily_update 任务，进度看上方任务区）。 */
export default function GapsPanel() {
  const { data, error, isLoading, mutate } = useSWR<GapsResp>(
    '/data/gaps?days=30',
    fetcher,
    { refreshInterval: 300_000 },
  );
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');

  async function repair(days: number) {
    setBusy('repair');
    setMsg('');
    try {
      const r = await post<{ created: boolean; task_id: string | null; reason: string | null; message?: string }>(
        '/data/gaps/repair', { days },
      );
      if (r.created) {
        setMsg(`✓ 已创建补采任务 ${r.task_id}，进度见下方「数据任务」`);
      } else if (r.reason === 'no_gap') {
        setMsg('✓ 没有缺口，无需补采');
        void mutate();
      } else {
        setMsg(`✗ 暂不能补采：${r.message ?? r.reason ?? '已有未完成任务'}`);
      }
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  const anyMissing = (data?.datasets ?? []).some((d) => d.missing.length > 0);

  return (
    <Panel
      title="数据缺口"
      meta={data ? `${data.window.start} ~ ${data.window.end} · 交易日口径` : undefined}
      actions={
        anyMissing && (
          <button className="btn btn-sm btn-primary" onClick={() => repair(30)} disabled={busy !== ''}>
            {busy === 'repair' ? '创建中…' : '一键补采全部缺口'}
          </button>
        )
      }
    >
      <Msg text={msg} />
      {error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-6 text-sm text-ink-faint">加载中…</p>
      ) : !data?.datasets.length ? (
        <Empty>暂无数据集 —— 数据湖为空</Empty>
      ) : (
        <div className="divide-line divide-y">
          {data.datasets.map((d) => {
            const ok = d.missing.length === 0;
            return (
              <div key={d.dataset} className="flex items-center justify-between gap-3 py-2 text-sm">
                <div className="min-w-0">
                  <span className={`mr-2 inline-block h-2 w-2 rounded-full align-middle ${ok ? 'bg-down' : 'bg-gold'}`} />
                  <span className="font-medium">{d.label}</span>
                  <span className={`ml-2 tabular-nums ${ok ? 'text-ink-faint' : 'text-gold'}`}>
                    {d.actual_days}/{d.expected_days} 天
                  </span>
                  {d.missing.length > 0 && (
                    <>
                      <span className="ml-2 text-xs text-gold">
                        缺 {d.missing.length} 天{d.missing.length <= 3 ? `（${d.missing.join('、')}）` : `（${d.missing[0]} 起）`}
                      </span>
                      {d.sparse_total > 0 && (
                        <span className="ml-2 text-xs text-ink-faint">{d.sparse_total} 只标的窗口内有稀疏缺口</span>
                      )}
                    </>
                  )}
                </div>
                {!ok && d.dataset === 'daily' && (
                  <button className="btn btn-sm shrink-0" onClick={() => repair(30)} disabled={busy !== ''}>
                    补采
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
