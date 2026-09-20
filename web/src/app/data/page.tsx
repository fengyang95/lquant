'use client';

import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import LazySection from '@/components/LazySection';
import { fetcher } from '@/lib/api';
import FreshnessHealthCard from './FreshnessHealthCard';
import ChecksPanel from './ChecksPanel';
import RecentIssuesPanel from './RecentIssuesPanel';
import CoverageMonthlyChart from './CoverageMonthlyChart';
import DataVersionCard from './DataVersionCard';
import DataDictionaryModal from './DataDictionaryModal';
import { useState } from 'react';

type Cover = {
  tables: { table: string; label: string; rows: number | null; latest: string | null; error?: boolean }[];
  daily_lake: { rows: number; symbols: number; start: string | null; end: string | null; error?: boolean };
};

export default function DataPage() {
  const { data, error, isLoading } = useSWR<Cover>('/data/coverage', fetcher);
  const [showDict, setShowDict] = useState(false);

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

  const lake = data?.daily_lake;
  const totalRows = data?.tables.reduce((a, t) => a + (t.rows ?? 0), 0) ?? 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="数据概览"
        sub={`数据湖与采集表健康度 · 共 ${totalRows.toLocaleString()} 行`}
        actions={
          <>
            <button onClick={() => setShowDict(true)} className="btn">数据字典</button>
            <a href="/sync" className="btn btn-primary">
              同步操作 →
            </a>
          </>
        }
      />

      {/* 顶部健康度总卡：新鲜度 + 近 30 日缺口 */}
      <FreshnessHealthCard />

      {/* 完备性检查结果（各 kind 最近一次同步后检查） */}
      <LazySection><ChecksPanel /></LazySection>

      {/* 覆盖率月度热力图（按月平均标的数，环比跌幅标金） */}
      <LazySection><CoverageMonthlyChart /></LazySection>

      {/* 最近 fatal/error 质量问题，点击展开详情 */}
      <LazySection><RecentIssuesPanel /></LazySection>

      <DataVersionCard />

      {/* 日线数据湖规模 */}
      <Panel title="日线数据湖" meta="Parquet">
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

      {/* 采集表覆盖度 */}
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

      {showDict && <DataDictionaryModal onClose={() => setShowDict(false)} />}
    </div>
  );
}
