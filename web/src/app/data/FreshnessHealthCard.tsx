'use client';

import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { fetcher } from '@/lib/api';
import { useMemo } from 'react';

/** GET /sync/freshness（lquant.sync.manager.freshness）。任一项可能为 null=未知 */
export type Freshness = {
  daily_lake: string | null;
  news: { latest: string | null; today_rows: number } | null;
  financial_pit: { covered_end: string | null; symbols?: number; marked?: number } | null;
  lag_days: number | null;
};

/** GET /data/gaps?days=30 的数据集切片 */
export type GapsDataset = {
  dataset: string;
  label: string;
  expected_days: number;
  actual_days: number;
  missing: string[];
  sparse_total: number;
};

/** lag=0 绿 / 1 黄 / >1 红 / null 灰（未知） */
export function lagTone(lag: number | null | undefined): 'down' | 'gold' | 'up' | 'faint' {
  if (lag == null || Number.isNaN(lag)) return 'faint';
  if (lag <= 0) return 'down';
  if (lag === 1) return 'gold';
  return 'up';
}

const TONE_DOT: Record<string, string> = {
  down: 'bg-down',
  gold: 'bg-gold',
  up: 'bg-up',
  faint: 'bg-ink-faint',
};

const TONE_TEXT: Record<string, string> = {
  down: 'text-down',
  gold: 'text-gold',
  up: 'text-up',
  faint: 'text-ink-faint',
};

/** 状态圆点：仅示意颜色，语义读 aria-label */
export function Dot({ tone, label }: { tone: string; label: string }) {
  return (
    <span
      aria-label={label}
      role="img"
      className={`inline-block h-2.5 w-2.5 rounded-full ${TONE_DOT[tone] ?? TONE_DOT.faint}`}
    />
  );
}

function latestDay(v: string | null | undefined): string {
  return v ? v.slice(0, 10) : '—';
}

/** 顶部健康度总卡：各数据源最新日期 + 落后交易日数 + 近 30 日缺口。
 *  只读可视化 —— 操作（补采/重跑）去 /sync。 */
export default function FreshnessHealthCard() {
  const { data: fresh, error: freshErr } = useSWR<Freshness>('/sync/freshness', fetcher, {
    refreshInterval: 60_000,
    shouldRetryOnError: false,
  });
  const { data: gaps } = useSWR<{ datasets: GapsDataset[] }>('/data/gaps?days=30', fetcher, {
    refreshInterval: 300_000,
    shouldRetryOnError: false,
  });

  const gapRows = useMemo(
    () =>
      (gaps?.datasets ?? [])
        .map((d) => ({ ...d, count: d.missing.length }))
        .filter((d) => d.count > 0),
    [gaps],
  );

  const view = freshErr ? undefined : fresh;
  const lag = view?.lag_days ?? null;
  const lakeTone = lagTone(lag);
  const newsTone = lagTone(view?.news ? (view.news.today_rows > 0 ? 0 : null) : null);
  const finTone = lagTone(view?.financial_pit ? 0 : null);

  return (
    <Panel
      title="数据新鲜度"
      meta="最新到哪一天 · lag=落后交易日数（绿 0 / 黄 1 / 红 ≥2 / 灰未知）"
      actions={
        <a href="/sync" className="text-xs text-ink-faint underline hover:text-ink">
          前往同步操作 →
        </a>
      }
    >
      {(
        <div className="grid gap-y-4 divide-line sm:grid-cols-3 sm:divide-x">
          <div className="sm:pr-4">
            <div className="flex items-center gap-2">
              <Dot tone={lakeTone} label={`日线湖状态：${lakeTone}`} />
              <span className="text-xs text-ink-faint">日线湖</span>
            </div>
            <div className="mt-0.5 font-song text-[22px] font-semibold leading-tight tabular-nums text-ink">
              {latestDay(view?.daily_lake)}
            </div>
            <div className="mt-0.5 text-xs text-ink-faint">
              {lag == null ? '落后交易日数未知' : `落后 ${lag} 个交易日`}
            </div>
          </div>
          <div className="sm:px-4">
            <div className="flex items-center gap-2">
              <Dot tone={newsTone} label={`资讯状态：${newsTone}`} />
              <span className="text-xs text-ink-faint">资讯</span>
            </div>
            <div className="mt-0.5 font-song text-[22px] font-semibold leading-tight tabular-nums text-ink">
              {view?.news ? latestDay(view.news.latest) : '—'}
            </div>
            <div className="mt-0.5 text-xs text-ink-faint">
              {view?.news ? `今日入库 ${view.news.today_rows} 条` : '暂无资讯数据'}
            </div>
          </div>
          <div className="sm:px-4">
            <div className="flex items-center gap-2">
              <Dot tone={finTone} label={`财务数据状态：${finTone}`} />
              <span className="text-xs text-ink-faint">财务数据</span>
            </div>
            <div className="mt-0.5 font-song text-[22px] font-semibold leading-tight tabular-nums text-ink">
              {view?.financial_pit ? latestDay(view.financial_pit.covered_end) : '—'}
            </div>
            <div className="mt-0.5 text-xs text-ink-faint">
              {view?.financial_pit ? `已覆盖 ${view.financial_pit.marked ?? view.financial_pit.symbols ?? 0} 只` : '暂无覆盖区间'}
            </div>
          </div>
        </div>
      )}

      {/* 近 30 日缺口：只列有缺的数据集，全绿则不出现在 DOM */}
      {gapRows.length > 0 && (
        <div className="mt-3 border-t border-line pt-3">
          <div className="mb-1 text-xs text-ink-faint">近 30 日缺口（点击去 /sync 补采）</div>
          <div className="flex flex-wrap gap-2">
            {gapRows.map((d) => (
              <a
                key={d.dataset}
                href="/sync"
                className="border border-up/40 bg-up/10 px-2 py-1 text-xs text-up"
              >
                {d.label || d.dataset} 缺 {d.count} 日
              </a>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}
