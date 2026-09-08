'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { fetcher, post } from '@/lib/api';

type Cover = {
  tables: { table: string; label: string; rows: number | null; latest: string | null; error?: boolean }[];
  daily_lake: { rows: number; symbols: number; start: string | null; end: string | null; error?: boolean };
};

type CollectHealth = {
  checked_at: string;
  any_gap: boolean;
  jobs: {
    job: string;
    label: string;
    schedule: string | null;
    runs_30d: number;
    success_rate: number | null;
    last_success: string | null;
    last_rows: number | null;
    today_gap: boolean;
  }[];
};

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-4">
      <div className="mb-2 text-xs font-medium text-neutral-500">{title}</div>
      {children}
    </div>
  );
}

export default function DataPage() {
  const { data, error, isLoading, mutate } = useSWR<Cover>('/data/coverage', fetcher);
  const { data: health } = useSWR<CollectHealth>('/market/collect-status', fetcher, {
    refreshInterval: 60_000,
  });
  const [busy, setBusy] = useState<'' | 'demo' | 'live'>('');
  const [msg, setMsg] = useState('');

  async function collect(demo: boolean) {
    setBusy(demo ? 'demo' : 'live');
    setMsg('');
    try {
      const r = await post<{ collected: Record<string, number>; persisted: Record<string, number>; errors: Record<string, string> }>(
        '/market/collect', demo ? { demo: true } : {},
      );
      const saved = Object.values(r.persisted).reduce((a, b) => a + b, 0);
      const nErr = Object.keys(r.errors ?? {}).length;
      setMsg(`✓ 采集完成：落库 ${saved} 行${demo ? '（demo 合成数据）' : ''}${nErr ? `，${nErr} 个采集器失败` : ''}`);
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  if (isLoading) return <div className="py-20 text-center text-neutral-400">加载中…</div>;
  if (error) return <div className="py-20 text-center text-red-500">加载失败：{String(error)}</div>;

  const lake = data?.daily_lake;
  const totalRows = data?.tables.reduce((a, t) => a + (t.rows ?? 0), 0) ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">数据</h1>
        <div className="flex gap-2">
          <button
            onClick={() => collect(true)}
            disabled={busy !== ''}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-40"
          >
            {busy === 'demo' ? '采集中…' : '采今日看板（demo）'}
          </button>
          <button
            onClick={() => collect(false)}
            disabled={busy !== ''}
            className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white hover:bg-neutral-700 disabled:opacity-40"
          >
            {busy === 'live' ? '采集中…' : '采今日看板（真实源）'}
          </button>
        </div>
      </div>
      {msg && <div className="rounded-md border bg-white px-4 py-2 text-sm">{msg}</div>}

      <Card title="日线数据湖（Parquet）">
        {lake?.rows ? (
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <div><div className="text-xs text-neutral-400">总行数</div><div className="font-semibold tabular-nums">{lake.rows.toLocaleString()}</div></div>
            <div><div className="text-xs text-neutral-400">标的数</div><div className="font-semibold tabular-nums">{lake.symbols.toLocaleString()}</div></div>
            <div><div className="text-xs text-neutral-400">起始日</div><div className="font-semibold tabular-nums">{lake.start}</div></div>
            <div><div className="text-xs text-neutral-400">最新日</div><div className="font-semibold tabular-nums">{lake.end}</div></div>
          </div>
        ) : (
          <div className="py-4 text-center text-sm text-neutral-400">
            数据湖为空 —— 跑 <code className="mx-1 rounded bg-neutral-100 px-1">./lquant.sh bootstrap</code> 或{' '}
            <code className="mx-1 rounded bg-neutral-100 px-1">lq data demo</code> 生成
          </div>
        )}
      </Card>

      {/* M9 采集健康度 */}
      {health && (
        <Card title={`采集健康度（${health.checked_at.slice(11, 16)} 检查）${health.any_gap ? ' · ⚠ 有当日缺口' : ''}`}>
          <div className="grid gap-2 md:grid-cols-2">
            {health.jobs.map((j) => (
              <div key={j.job} className={`flex items-center justify-between rounded-lg border px-3 py-2 text-sm ${j.today_gap ? 'border-orange-300 bg-orange-50' : ''}`}>
                <div>
                  <span className="font-medium">{j.label}</span>
                  <span className="ml-2 text-xs text-neutral-400">{j.schedule}</span>
                  {j.today_gap && <span className="ml-2 text-xs text-orange-600">当日缺口（易失数据不可回溯）</span>}
                </div>
                <div className="text-right text-xs tabular-nums">
                  <span className={j.success_rate == null ? 'text-neutral-300' : j.success_rate >= 0.9 ? 'text-up' : 'text-orange-600'}>
                    {j.success_rate == null ? '—' : `${(j.success_rate * 100).toFixed(0)}%`}
                  </span>
                  <span className="ml-2 text-neutral-400">{j.last_success ? `最近 ${j.last_success.slice(5)}` : '从未成功'}</span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
        {data?.tables.map((t) => (
          <div key={t.table} className="rounded-lg border bg-white p-3">
            <div className="text-xs text-neutral-400">{t.label || t.table}</div>
            <div className={`mt-1 text-lg font-semibold tabular-nums ${t.rows ? '' : 'text-neutral-300'}`}>
              {t.rows == null ? '✗' : t.rows.toLocaleString()}
            </div>
            <div className="mt-0.5 text-xs text-neutral-400">
              {t.error ? '读取失败' : t.latest ? `最新 ${t.latest}` : t.rows === 0 ? '空表' : '—'}
            </div>
          </div>
        ))}
      </div>

      <div className="text-xs text-neutral-400">
        共 {totalRows.toLocaleString()} 行 · 历史日线/财务/分钟线批量补数走 CLI（<code className="rounded bg-neutral-100 px-1">lq data --help</code>），
        盘后看板数据由调度器自动采集，也可用右上角按钮手动触发。
      </div>
    </div>
  );
}
