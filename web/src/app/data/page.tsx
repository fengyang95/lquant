'use client';

import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
import LazySection from '@/components/LazySection';
import Chart from '@/components/Chart';
import { fetcher, post } from '@/lib/api';
import TasksPanel from './TasksPanel';
import SourceConfigPanel from './SourceConfigPanel';
import CrosscheckPanel from './CrosscheckPanel';
import QualityPanel from './QualityPanel';
import CheckpointPanel from './CheckpointPanel';
import CoverageMonthlyChart from './CoverageMonthlyChart';
import IssuesPanel from './IssuesPanel';
import SyncJobsPanel from './SyncJobsPanel';
import DataVersionCard from './DataVersionCard';
import QlibExportCard from './QlibExportCard';
import GapsPanel from './GapsPanel';
import PurgeModal from './PurgeModal';
import DataDictionaryModal from './DataDictionaryModal';

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

export default function DataPage() {
  const { data, error, isLoading, mutate } = useSWR<Cover>('/data/coverage', fetcher);
  const { data: health } = useSWR<CollectHealth>('/market/collect-status', fetcher, {
    refreshInterval: 60_000,
  });
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 卸载后不再触发延迟刷新，避免对已卸载组件调 mutate
  useEffect(() => () => {
    if (reloadTimer.current) clearTimeout(reloadTimer.current);
  }, []);
  const [busy, setBusy] = useState<'' | 'demo' | 'live' | 'ref'>('');
  const [msg, setMsg] = useState('');
  const [showPurge, setShowPurge] = useState(false);
  const [showDict, setShowDict] = useState(false);

  /** 导出日线 CSV：拼 URL 新窗口下载（GET /data/export 返回文件流） */
  function exportCsv() {
    window.open('/api/data/export?dataset=daily&format=csv', '_blank');
  }

  async function syncReference() {
    setBusy('ref');
    setMsg('');
    try {
      const r = await post<{ accepted: boolean; sync_details: boolean }>('/data/reference/sync', {});
      setMsg(
        `✓ 标的清单同步已开始（含退市股${r.sync_details ? ' + 详情补齐' : ''}），后台执行中，可稍后刷新查看退市股是否入表`,
      );
      reloadTimer.current = setTimeout(() => mutate(), 30_000);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

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

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

  const lake = data?.daily_lake;
  const totalRows = data?.tables.reduce((a, t) => a + (t.rows ?? 0), 0) ?? 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="数据"
        sub={`数据湖与采集表覆盖度 · 共 ${totalRows.toLocaleString()} 行`}
        actions={
          <>
            <button onClick={() => setShowDict(true)} className="btn">数据字典</button>
            <button onClick={syncReference} disabled={busy !== ''} className="btn">
              {busy === 'ref' ? '同步中…' : '同步全市场清单（含退市）'}
            </button>
            <button onClick={() => collect(true)} disabled={busy !== ''} className="btn">
              {busy === 'demo' ? '采集中…' : '采今日看板（demo）'}
            </button>
            <button onClick={() => collect(false)} disabled={busy !== ''} className="btn btn-primary">
              {busy === 'live' ? '采集中…' : '采今日看板（真实源）'}
            </button>
          </>
        }
      />
      <Msg text={msg} />

      <DataVersionCard />

      <LazySection><QlibExportCard /></LazySection>

      {/* 日线数据补全：任务进度 / 断点续传 / 数据源配置 / 跨源印证 / 全湖质量检查。
          折叠线以下全部懒挂载：进入视口前不发请求，首屏瞬时请求从 9 路降到 2 路 */}
      <LazySection><GapsPanel /></LazySection>
      <LazySection><TasksPanel /></LazySection>
      <LazySection><CheckpointPanel /></LazySection>
      <LazySection><SourceConfigPanel /></LazySection>
      <LazySection><CrosscheckPanel /></LazySection>
      <LazySection><QualityPanel /></LazySection>
      <LazySection><IssuesPanel /></LazySection>
      <LazySection><SyncJobsPanel /></LazySection>

      <Panel
        title="日线数据湖"
        meta="Parquet"
        actions={
          <>
            <button onClick={exportCsv} className="btn btn-sm">导出 CSV</button>
            <button onClick={() => setShowPurge(true)} className="btn btn-sm">数据清理</button>
          </>
        }
      >
        {lake?.rows ? (
          <div className="grid grid-cols-2 gap-y-4 divide-line sm:grid-cols-4 sm:divide-x">
            <div className="sm:pr-4">
              <Stat label="总行数" value={lake.rows.toLocaleString()} />
            </div>
            <div className="sm:px-4">
              <Stat label="标的数" value={lake.symbols.toLocaleString()} />
            </div>
            <div className="sm:px-4">
              <Stat label="起始日" value={lake.start ?? '—'} />
            </div>
            <div className="sm:px-4">
              <Stat label="最新日" value={lake.end ?? '—'} />
            </div>
          </div>
        ) : (
          <Empty>
            数据湖为空 —— 跑 <code className="bg-paper px-1">./lquant.sh bootstrap</code> 或{' '}
            <code className="bg-paper px-1">lq data demo</code> 生成
          </Empty>
        )}
      </Panel>

      {/* M9 采集健康度 */}
      {health && (
        <Panel
          title="采集健康度"
          meta={`${health.checked_at.slice(11, 16)} 检查${health.any_gap ? ' · 有当日缺口' : ''}`}
        >
          <CollectHealthChart jobs={health.jobs} />
          <div className="grid gap-2 md:grid-cols-2">
            {health.jobs.map((j) => (
              <div
                key={j.job}
                className={`flex items-center justify-between border px-3 py-2 text-sm ${
                  j.today_gap ? 'border-gold/40 bg-[#F7EFE6]' : 'border-line bg-panel'
                }`}
              >
                <div className="min-w-0">
                  <span className="font-medium">{j.label}</span>
                  <span className="ml-2 text-xs text-ink-faint">{j.schedule}</span>
                  {j.today_gap && <span className="ml-2 text-xs text-gold">当日缺口（易失数据不可回溯）</span>}
                </div>
                <div className="shrink-0 text-right text-xs tabular-nums">
                  <span className={j.success_rate == null ? 'text-ink-faint' : j.success_rate >= 0.9 ? 'text-down' : 'text-gold'}>
                    {j.success_rate == null ? '—' : `${(j.success_rate * 100).toFixed(0)}%`}
                  </span>
                  <span className="ml-2 text-ink-faint">{j.last_success ? `最近 ${j.last_success.slice(5)}` : '从未成功'}</span>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <LazySection><CoverageMonthlyChart /></LazySection>

      <Panel title="采集表覆盖度">
        {!data?.tables.length ? (
          <Empty>暂无采集表</Empty>
        ) : (
          <div className="grid grid-cols-2 gap-y-4 divide-line sm:grid-cols-3 lg:grid-cols-6 sm:divide-x">
            {data.tables.map((t) => (
              <div key={t.table} className="sm:px-4 first:sm:pl-0">
                <Stat
                  label={t.label || t.table}
                  value={t.rows == null ? '✗' : t.rows.toLocaleString()}
                  tone={t.rows ? undefined : 'text-ink-faint'}
                  hint={t.error ? '读取失败' : t.latest ? `最新 ${t.latest}` : t.rows === 0 ? '空表' : '—'}
                />
              </div>
            ))}
          </div>
        )}
      </Panel>

      <div className="text-xs text-ink-faint">
        共 {totalRows.toLocaleString()} 行 · 日线全量回填/增量在上方「数据任务」区触发，
        盘后看板数据由调度器自动采集，也可用右上角按钮手动触发；
        历史分钟线/财务批量补数走 CLI（<code className="bg-paper px-1">lq data --help</code>）。
      </div>

      {showPurge && (
        <PurgeModal
          onClose={() => setShowPurge(false)}
          onPurged={(r) => {
            setShowPurge(false);
            setMsg(`✓ 已删除 ${r.rows_matched.toLocaleString()} 行${r.files?.length ? `（${r.files.length} 个文件）` : ''}，重新回填后恢复`);
            mutate();
          }}
        />
      )}
      {showDict && <DataDictionaryModal onClose={() => setShowDict(false)} />}
    </div>
  );
}

/** 采集健康度成功率横向条形图：null 视为 0（从未成功），≥90% 用涨色，其余警示金 */
function CollectHealthChart({ jobs }: { jobs: CollectHealth['jobs'] }) {
  const valid = jobs.filter((j) => j.success_rate != null);
  if (!valid.length) return null;
  const sorted = [...valid].sort((a, b) => (a.success_rate ?? 0) - (b.success_rate ?? 0));
  return (
    <div className="mb-3">
      <Chart
        height={Math.max(120, sorted.length * 30)}
        option={{
          grid: { left: 8, right: 48, top: 4, bottom: 4, containLabel: true },
          tooltip: { trigger: 'item' as const },
          xAxis: {
            type: 'value' as const,
            max: 1,
            axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` },
          },
          yAxis: {
            type: 'category' as const,
            data: sorted.map((j) => j.label),
            axisTick: { show: false },
          },
          series: [{
            type: 'bar' as const,
            barWidth: 12,
            data: sorted.map((j) => ({
              value: j.success_rate,
              itemStyle: { color: (j.success_rate ?? 0) >= 0.9 ? '#0f8a5f' : '#c07f00' },
            })),
            label: {
              show: true, position: 'right' as const,
              formatter: ({ value }: { value: number }) =>
                value == null ? '—' : `${Math.round(value * 100)}%`,
            },
          }],
        }}
      />
    </div>
  );
}
