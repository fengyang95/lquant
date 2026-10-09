'use client';

import { useState } from 'react';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import LazySection from '@/components/LazySection';
import { get, post, del } from '@/lib/api';
import RetryBadge from './RetryBadge';
import { runRetryText } from './retry';
import type { Freshness } from '../data/FreshnessHealthCard';
import { Dot, lagTone } from '../data/FreshnessHealthCard';
import CrosscheckPanel from '../data/CrosscheckPanel';
import GapsPanel from '../data/GapsPanel';
import CheckpointPanel from '../data/CheckpointPanel';
import SourceConfigPanel from '../data/SourceConfigPanel';
import PurgeModal from '../data/PurgeModal';

type SyncJob = {
  sync_id: string;
  name: string;
  kind: string; // collect | daily | adj_factor | daily_basic | financial
  schedule_time: string;
  weekdays: string;
  params: Record<string, unknown>;
  enabled: boolean;
  last_run_at: string | null;
  last_status: string | null;
  last_rows: number | null;
  retry_count: number | null;
  next_retry_at: string | null;
};

type SyncRun = {
  run_id: string; sync_id: string; job_name: string; kind: string;
  started_at: string; finished_at: string; rows: number; status: string;
  detail: Record<string, unknown>;
};

const KIND_LABEL: Record<string, string> = {
  collect: '市场采集', daily: '日线增量', adj_factor: '复权因子',
  daily_basic: '基本面增量', financial: '财务数据',
};
const WD_LABEL = ['一', '二', '三', '四', '五', '六', '日'];

function wdText(w: string): string {
  const xs = w.split(',').map((x) => parseInt(x, 10)).filter((x) => x >= 1 && x <= 7);
  if (!xs.length) return '每天';
  if (xs.length === 7) return '每天';
  if (xs.join(',') === '1,2,3,4,5') return '工作日';
  return xs.map((x) => WD_LABEL[x - 1]).join('·');
}

/** 作业状态小标签：方角描边，token 配色（成功=绿） */
function StatusTag({ job }: { job: SyncJob }) {
  const base = 'inline-block rounded-[2px] border px-1.5 py-0.5 text-xs';
  if (!job.enabled) return <span className={`${base} border-line-strong text-ink-faint`}>已停用</span>;
  if (job.last_status === 'ok') return <span className={`${base} border-down/40 text-down`}>ok</span>;
  if (job.last_status === 'partial') return <span className={`${base} border-gold/40 text-gold`}>partial</span>;
  if (job.last_status === 'failed') return <span className={`${base} border-up/40 text-up`}>failed</span>;
  return <span className={`${base} border-indigo/40 text-indigo`}>{job.last_status ?? '待运行'}</span>;
}

export default function SyncPage() {
  const { data: jobs, mutate: mutateJobs } = useSWR<SyncJob[]>('/sync/jobs', get, { refreshInterval: 10_000 });
  const { data: hist, mutate: mutateHist } = useSWR<SyncRun[]>('/sync/history?limit=30', get, { refreshInterval: 15_000 });
  const { data: fresh } = useSWR<Freshness>('/sync/freshness', get, { refreshInterval: 60_000, shouldRetryOnError: false });

  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [highlight, setHighlight] = useState('');
  const [showPurge, setShowPurge] = useState(false);
  // 新建/编辑表单
  const [form, setForm] = useState({ sync_id: '', name: '', kind: 'collect', schedule_time: '15:05', weekdays: '1,2,3,4,5', schedule: 'close' });

  /** 摘要卡点击 → 滚动到作业行并短暂高亮（timeout 自清理） */
  function scrollToJob(id: string) {
    setHighlight(id);
    document.getElementById(`job-row-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

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
    setMsg('');
    try {
      await post(`/sync/jobs/${j.sync_id}/toggle`, { enabled: !j.enabled });
      mutateJobs();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally { setBusy(''); }
  }

  async function remove(id: string) {
    setBusy(id);
    setMsg('');
    try {
      await del(`/sync/jobs/${id}`);
      mutateJobs();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
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

  /** 失败 / 重试中的作业 → 摘要卡红色项 */
  const problemJobs = (jobs ?? []).filter(
    (j) => j.enabled && (j.last_status === 'failed' || (j.retry_count ?? 0) > 0 || j.next_retry_at != null),
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="同步操作"
        sub="后台 worker 每 30s 检查到期作业 · 到点自动执行 · 失败自动重试"
        actions={
          <button onClick={() => setShowPurge(true)} className="btn">
            数据清理
          </button>
        }
      />

      {/* 健康度摘要卡：新鲜度 + 问题作业直达 */}
      <Panel title="健康度摘要" meta="失败 / 重试中的作业点击直达作业行">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div className="flex items-center gap-2">
            <Dot tone={lagTone(fresh?.lag_days ?? null)} label="日线湖新鲜度" />
            <span className="text-ink-faint">日线湖</span>
            <span className="tabular-nums">{fresh?.daily_lake ? fresh.daily_lake.slice(0, 10) : '—'}</span>
            <span className="text-xs text-ink-faint">
              {fresh?.lag_days == null ? '' : `落后 ${fresh.lag_days} 交易日`}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Dot tone={lagTone(fresh?.news ? (fresh.news.today_rows > 0 ? 0 : null) : null)} label="资讯新鲜度" />
            <span className="text-ink-faint">资讯</span>
            <span className="tabular-nums">{fresh?.news?.latest ? fresh.news.latest.slice(0, 10) : '—'}</span>
          </div>
          {problemJobs.length === 0 ? (
            <span className="text-xs text-ink-faint">无失败 / 重试作业</span>
          ) : (
            problemJobs.map((j) => (
              <button
                key={j.sync_id}
                type="button"
                onClick={() => scrollToJob(j.sync_id)}
                className="border border-up/40 bg-up/5 px-2 py-1 text-xs text-up hover:bg-up/10"
              >
                {j.name}（{j.last_status === 'failed' ? '失败' : '重试中'}）
              </button>
            ))
          )}
        </div>
      </Panel>

      <Msg text={msg} />

      {/* 作业列表 */}
      <Panel title="同步作业" meta={`${jobs?.length ?? 0} 个`}>
        {!jobs?.length ? (
          <Empty>还没有作业 —— 下方表单新建一个</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">作业</th>
                <th className="text-left">类型</th>
                <th className="text-left">计划</th>
                <th className="text-left">上次运行</th>
                <th className="text-left">状态</th>
                <th className="text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr
                  key={j.sync_id}
                  id={`job-row-${j.sync_id}`}
                  className={highlight === j.sync_id ? 'bg-gold/10' : 'hover:bg-white'}
                >
                  <td>
                    <div className="font-medium">{j.name}</div>
                    <div className="font-mono text-xs text-ink-faint">{j.sync_id}</div>
                  </td>
                  <td className="text-xs">{KIND_LABEL[j.kind] ?? j.kind}</td>
                  <td className="text-xs">
                    <span className="font-mono">{j.schedule_time}</span>
                    <span className="ml-1 text-ink-faint">{wdText(j.weekdays)}</span>
                    {j.kind === 'collect' && j.params?.schedule != null && (
                      <span className="ml-1 text-ink-faint">({String(j.params.schedule)})</span>
                    )}
                  </td>
                  <td className="text-xs tabular-nums text-ink-dim">
                    {j.last_run_at?.slice(5, 16) ?? '—'}
                    <span className="ml-1 text-ink-faint">{j.last_rows != null ? `${j.last_rows}行` : ''}</span>
                  </td>
                  <td>
                    <StatusTag job={j} />
                    <RetryBadge retryCount={j.retry_count} nextRetryAt={j.next_retry_at} />
                  </td>
                  <td className="text-right">
                    <button onClick={() => runNow(j.sync_id)} disabled={busy === j.sync_id} className="btn btn-sm">
                      {busy === j.sync_id ? '…' : '立即运行'}
                    </button>
                    <button onClick={() => toggle(j)} disabled={busy === j.sync_id} className="btn btn-sm ml-1">
                      {j.enabled ? '停用' : '启用'}
                    </button>
                    <button onClick={() => remove(j.sync_id)} disabled={busy === j.sync_id}
                            className="btn btn-sm ml-1 border-up/40 text-up hover:bg-paper">
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {/* 新建作业 */}
      <Panel title="新建 / 覆盖作业">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-ink-faint">ID
            <input value={form.sync_id} onChange={(e) => setForm({ ...form, sync_id: e.target.value })}
                   className="input input-mono mt-1 block w-28" />
          </label>
          <label className="text-xs text-ink-faint">名称
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                   className="input mt-1 block w-40" />
          </label>
          <label className="text-xs text-ink-faint">类型
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}
                    className="input mt-1 block">
              <option value="collect">市场采集</option>
              <option value="daily">日线增量</option>
              <option value="adj_factor">复权因子</option>
              <option value="daily_basic">基本面增量</option>
              <option value="financial">财务数据</option>
            </select>
          </label>
          <label className="text-xs text-ink-faint">时间
            <input value={form.schedule_time} onChange={(e) => setForm({ ...form, schedule_time: e.target.value })}
                   className="input input-mono mt-1 block w-20" />
          </label>
          <label className="text-xs text-ink-faint">周几(1-5)
            <input value={form.weekdays} onChange={(e) => setForm({ ...form, weekdays: e.target.value })}
                   className="input input-mono mt-1 block w-24" />
          </label>
          {form.kind === 'collect' && (
            <label className="text-xs text-ink-faint">采集时点
              <select value={form.schedule} onChange={(e) => setForm({ ...form, schedule: e.target.value })}
                      className="input mt-1 block">
                <option value="close">close（收盘）</option>
                <option value="evening">evening（盘后）</option>
                <option value="preopen">preopen（盘前）</option>
              </select>
            </label>
          )}
          <button onClick={create} disabled={busy === 'create' || !form.sync_id} className="btn btn-primary">
            {busy === 'create' ? '保存中…' : '保存'}
          </button>
        </div>
      </Panel>

      {/* 运行历史 */}
      <Panel title="运行历史" meta={`${hist?.length ?? 0} 条`}>
        {!hist?.length ? (
          <Empty>还没有运行记录 —— 点上面的「立即运行」试试</Empty>
        ) : (
          <div className="max-h-72 overflow-auto">
            <table className="table-dense">
              <thead className="sticky top-0 bg-panel">
                <tr>
                  <th className="text-left">开始时间</th>
                  <th className="text-left">作业</th>
                  <th className="text-left">类型</th>
                  <th className="text-right">行数</th>
                  <th className="text-left">状态</th>
                  <th className="text-left">重试</th>
                  <th className="text-left">详情</th>
                </tr>
              </thead>
              <tbody>
                {hist.map((h) => {
                  const retryTxt = runRetryText(h.detail);
                  return (
                    <tr key={h.run_id} className="hover:bg-white">
                      <td className="text-xs tabular-nums">{h.started_at.slice(5, 16)}</td>
                      <td className="text-xs">{h.job_name}</td>
                      <td className="text-xs">{KIND_LABEL[h.kind] ?? h.kind}</td>
                      <td className="text-right">{h.rows}</td>
                      <td className={`text-xs font-medium ${
                        h.status === 'ok' ? 'text-down' : h.status === 'partial' ? 'text-gold' : 'text-up'}`}>
                        {h.status}
                      </td>
                      <td className="text-xs text-gold">{retryTxt ?? '—'}</td>
                      <td className="max-w-64 truncate text-xs text-ink-faint" title={JSON.stringify(h.detail)}>
                        {h.detail && Object.keys(h.detail).length ? JSON.stringify(h.detail).slice(0, 80) : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* 数据质量运维 —— 原 /data 的操作类面板，随 PR #79「概览/操作页拆分」迁移至此。
          ChecksPanel 注释承诺的「修复动作（补齐/重跑）在 /sync」即由这一组面板兑现：
          跨源对账 / 缺口补采 / 断点归档 / 数据源配置，配套清理入口见页头「数据清理」。 */}
      <LazySection><CrosscheckPanel /></LazySection>
      <LazySection><GapsPanel /></LazySection>
      <LazySection><CheckpointPanel /></LazySection>
      <LazySection><SourceConfigPanel /></LazySection>

      {showPurge && (
        <PurgeModal
          onClose={() => setShowPurge(false)}
          onPurged={(r) => {
            setShowPurge(false);
            setMsg(`✓ 已清理 ${r.rows_matched.toLocaleString()} 行日线数据`);
            mutateJobs();
            mutateHist();
          }}
        />
      )}
    </div>
  );
}
