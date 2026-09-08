'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { get, post, del } from '@/lib/api';

type SyncJob = {
  sync_id: string;
  name: string;
  kind: 'collect' | 'daily' | 'adj_factor';
  schedule_time: string;
  weekdays: string;
  params: Record<string, unknown>;
  enabled: boolean;
  last_run_at: string | null;
  last_status: string | null;
  last_rows: number | null;
};

type SyncRun = {
  run_id: string; sync_id: string; job_name: string; kind: string;
  started_at: string; finished_at: string; rows: number; status: string;
  detail: Record<string, unknown>;
};

type Coverage = {
  lake: { rows: number; symbols: number; first_day: string | null; last_day: string | null };
  reference: { table: string; rows: number }[];
  market_tables: { table: string; rows: number; first_day: string | null; last_day: string | null }[];
};

const KIND_LABEL: Record<string, string> = {
  collect: '市场采集', daily: '日线增量', adj_factor: '复权因子',
};
const WD_LABEL = ['一', '二', '三', '四', '五', '六', '日'];

function wdText(w: string): string {
  const xs = w.split(',').map((x) => parseInt(x, 10)).filter((x) => x >= 1 && x <= 7);
  if (!xs.length) return '每天';
  if (xs.length === 7) return '每天';
  if (xs.join(',') === '1,2,3,4,5') return '工作日';
  return xs.map((x) => WD_LABEL[x - 1]).join('·');
}

export default function SyncPage() {
  const { data: jobs, mutate: mutateJobs } = useSWR<SyncJob[]>('/sync/jobs', get, { refreshInterval: 10_000 });
  const { data: hist, mutate: mutateHist } = useSWR<SyncRun[]>('/sync/history?limit=30', get, { refreshInterval: 15_000 });
  const { data: cov } = useSWR<Coverage>('/sync/coverage', get);

  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  // 新建/编辑表单
  const [form, setForm] = useState({ sync_id: '', name: '', kind: 'collect', schedule_time: '15:05', weekdays: '1,2,3,4,5', schedule: 'close' });

  async function runNow(id: string) {
    setBusy(id);
    setMsg('');
    try {
      const r = await post<{ status: string; rows: number }>('/sync/run', { sync_id: id, demo: true });
      setMsg(`✓ ${id} 执行完成：${r.status}，写入 ${r.rows} 行（demo 模式）`);
      mutateJobs(); mutateHist();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function toggle(j: SyncJob) {
    setBusy(j.sync_id);
    try {
      await post(`/sync/jobs/${j.sync_id}/toggle`, { enabled: !j.enabled });
      mutateJobs();
    } finally { setBusy(''); }
  }

  async function remove(id: string) {
    setBusy(id);
    try {
      await del(`/sync/jobs/${id}`);
      mutateJobs();
    } finally { setBusy(''); }
  }

  async function create() {
    setBusy('create');
    setMsg('');
    try {
      const params = form.kind === 'collect' ? { schedule: form.schedule } : {};
      await post('/sync/jobs', {
        sync_id: form.sync_id, name: form.name || form.sync_id, kind: form.kind,
        schedule_time: form.schedule_time, weekdays: form.weekdays, params,
      });
      setMsg(`✓ 已保存 ${form.sync_id}`);
      setForm({ ...form, sync_id: '', name: '' });
      mutateJobs();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally { setBusy(''); }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">数据同步</h1>
        <span className="text-xs text-neutral-400">后台 worker 每 30s 检查到期作业 · 到点自动执行 · 重启自动补跑</span>
      </div>

      {msg && <div className="rounded-md border bg-white px-4 py-2 text-sm">{msg}</div>}

      {/* 作业列表 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">同步作业（{jobs?.length ?? 0}）</div>
        <table className="w-full text-sm">
          <thead className="text-xs text-neutral-400">
            <tr className="border-b">
              <th className="py-1.5 text-left font-normal">作业</th>
              <th className="text-left font-normal">类型</th>
              <th className="text-left font-normal">计划</th>
              <th className="text-left font-normal">上次运行</th>
              <th className="text-left font-normal">状态</th>
              <th className="text-right font-normal">操作</th>
            </tr>
          </thead>
          <tbody>
            {(jobs ?? []).map((j) => (
              <tr key={j.sync_id} className="border-b border-neutral-50">
                <td className="py-2">
                  <div className="font-medium">{j.name}</div>
                  <div className="font-mono text-xs text-neutral-400">{j.sync_id}</div>
                </td>
                <td className="text-xs">{KIND_LABEL[j.kind] ?? j.kind}</td>
                <td className="text-xs">
                  <span className="font-mono">{j.schedule_time}</span>
                  <span className="ml-1 text-neutral-400">{wdText(j.weekdays)}</span>
                  {j.kind === 'collect' && j.params?.schedule && (
                    <span className="ml-1 text-neutral-400">({String(j.params.schedule)})</span>
                  )}
                </td>
                <td className="text-xs tabular-nums text-neutral-500">
                  {j.last_run_at?.slice(5, 16) ?? '—'}
                  <span className="ml-1 text-neutral-400">{j.last_rows != null ? `${j.last_rows}行` : ''}</span>
                </td>
                <td>
                  <span className={`inline-block rounded-full px-2 py-0.5 text-xs ${
                    !j.enabled ? 'bg-neutral-100 text-neutral-400'
                    : j.last_status === 'ok' ? 'bg-green-50 text-green-700'
                    : j.last_status === 'partial' ? 'bg-amber-50 text-amber-700'
                    : j.last_status === 'failed' ? 'bg-red-50 text-red-600'
                    : 'bg-blue-50 text-blue-600'}`}>
                    {!j.enabled ? '已停用' : j.last_status ?? '待运行'}
                  </span>
                </td>
                <td className="text-right">
                  <button onClick={() => runNow(j.sync_id)} disabled={busy === j.sync_id}
                          className="rounded border px-2 py-0.5 text-xs hover:bg-neutral-100 disabled:opacity-40">
                    {busy === j.sync_id ? '…' : '立即运行'}
                  </button>
                  <button onClick={() => toggle(j)} disabled={busy === j.sync_id}
                          className="ml-1 rounded border px-2 py-0.5 text-xs hover:bg-neutral-100 disabled:opacity-40">
                    {j.enabled ? '停用' : '启用'}
                  </button>
                  <button onClick={() => remove(j.sync_id)} disabled={busy === j.sync_id}
                          className="ml-1 rounded border px-2 py-0.5 text-xs text-red-500 hover:bg-red-50 disabled:opacity-40">
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 新建作业 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-3 text-sm font-medium">新建 / 覆盖作业</div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-neutral-400">ID
            <input value={form.sync_id} onChange={(e) => setForm({ ...form, sync_id: e.target.value })}
                   className="block w-28 rounded-md border px-2 py-1.5 text-sm text-neutral-900" />
          </label>
          <label className="text-xs text-neutral-400">名称
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                   className="block w-40 rounded-md border px-2 py-1.5 text-sm text-neutral-900" />
          </label>
          <label className="text-xs text-neutral-400">类型
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}
                    className="block rounded-md border px-2 py-1.5 text-sm text-neutral-900">
              <option value="collect">市场采集</option>
              <option value="daily">日线增量</option>
              <option value="adj_factor">复权因子</option>
            </select>
          </label>
          <label className="text-xs text-neutral-400">时间
            <input value={form.schedule_time} onChange={(e) => setForm({ ...form, schedule_time: e.target.value })}
                   className="block w-20 rounded-md border px-2 py-1.5 font-mono text-sm text-neutral-900" />
          </label>
          <label className="text-xs text-neutral-400">周几(1-5)
            <input value={form.weekdays} onChange={(e) => setForm({ ...form, weekdays: e.target.value })}
                   className="block w-24 rounded-md border px-2 py-1.5 font-mono text-sm text-neutral-900" />
          </label>
          {form.kind === 'collect' && (
            <label className="text-xs text-neutral-400">采集时点
              <select value={form.schedule} onChange={(e) => setForm({ ...form, schedule: e.target.value })}
                      className="block rounded-md border px-2 py-1.5 text-sm text-neutral-900">
                <option value="close">close（收盘）</option>
                <option value="evening">evening（盘后）</option>
                <option value="preopen">preopen（盘前）</option>
              </select>
            </label>
          )}
          <button onClick={create} disabled={busy === 'create' || !form.sync_id}
                  className="rounded-md bg-neutral-900 px-4 py-1.5 text-sm text-white hover:bg-neutral-700 disabled:opacity-40">
            {busy === 'create' ? '保存中…' : '保存'}
          </button>
        </div>
      </div>

      {/* 数据覆盖度 */}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium">Parquet 日线湖</div>
          {cov ? (
            <div className="space-y-1 text-sm">
              <div className="flex justify-between"><span className="text-neutral-400">行数</span><span className="tabular-nums">{cov.lake.rows.toLocaleString()}</span></div>
              <div className="flex justify-between"><span className="text-neutral-400">标的数</span><span className="tabular-nums">{cov.lake.symbols}</span></div>
              <div className="flex justify-between"><span className="text-neutral-400">起止</span>
                <span className="tabular-nums text-xs">{cov.lake.first_day ?? '—'} ~ {cov.lake.last_day ?? '—'}</span></div>
            </div>
          ) : <div className="py-8 text-center text-xs text-neutral-400">加载中…</div>}
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium">参考数据表</div>
          <table className="w-full text-xs">
            <tbody>
              {(cov?.reference ?? []).map((t) => (
                <tr key={t.table} className="border-b border-neutral-50">
                  <td className="py-1 font-mono">{t.table}</td>
                  <td className="text-right tabular-nums">{t.rows.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium">看板采集表</div>
          <table className="w-full text-xs">
            <tbody>
              {(cov?.market_tables ?? []).map((t) => (
                <tr key={t.table} className="border-b border-neutral-50">
                  <td className="py-1 font-mono">{t.table}</td>
                  <td className="text-right tabular-nums">{(t.rows ?? 0).toLocaleString()}</td>
                  <td className="text-right text-neutral-400">{t.last_day ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* 运行历史 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">运行历史（{hist?.length ?? 0}）</div>
        {!hist?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">还没有运行记录 —— 点上面的「立即运行」试试</div>
        ) : (
          <div className="max-h-72 overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-white text-xs text-neutral-400">
                <tr className="border-b">
                  <th className="py-1.5 text-left font-normal">开始时间</th>
                  <th className="text-left font-normal">作业</th>
                  <th className="text-left font-normal">类型</th>
                  <th className="text-right font-normal">行数</th>
                  <th className="text-left font-normal">状态</th>
                  <th className="text-left font-normal">详情</th>
                </tr>
              </thead>
              <tbody>
                {hist.map((h) => (
                  <tr key={h.run_id} className="border-b border-neutral-50">
                    <td className="py-1.5 tabular-nums text-xs">{h.started_at.slice(5, 16)}</td>
                    <td className="text-xs">{h.job_name}</td>
                    <td className="text-xs">{KIND_LABEL[h.kind] ?? h.kind}</td>
                    <td className="text-right tabular-nums">{h.rows}</td>
                    <td>
                      <span className={`text-xs font-medium ${
                        h.status === 'ok' ? 'text-green-600'
                        : h.status === 'partial' ? 'text-amber-600' : 'text-red-500'}`}>
                        {h.status}
                      </span>
                    </td>
                    <td className="max-w-64 truncate text-xs text-neutral-400" title={JSON.stringify(h.detail)}>
                      {h.detail && Object.keys(h.detail).length ? JSON.stringify(h.detail).slice(0, 80) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
