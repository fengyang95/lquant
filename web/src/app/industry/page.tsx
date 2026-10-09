'use client';

/**
 * 行业轮动榜 —— 行业分析的主屏。
 *
 * 回答四个问题：**谁在领跑（区间收益）、钱在哪（成交额）、贵不贵（PE 中位数）、
 * 有几个成员（代表性）**。点任意一行进入该行业的多角度分析。
 *
 * 排名列统一走 `rank` 字段，不由前端重排 —— 后端按同一 PIT 口径算的收益与
 * 排名必须一致，前端再排一次就会出现「第 3 名显示在第一行」。
 */
import { useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { fetcher } from '@/lib/api';
import type { RotationResponse, RotationRow } from '@/lib/industry';

const WINDOWS = [
  { key: 20, label: '20 日' },
  { key: 60, label: '60 日' },
  { key: 120, label: '120 日' },
] as const;

function fmtYi(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${(v / 1e8).toFixed(0)} 亿`;
}

function Pct({ v }: { v: number | null }) {
  if (v == null) return <span className="text-ink-faint">—</span>;
  return (
    <span className={v >= 0 ? 'text-up' : 'text-down'}>
      {v >= 0 ? '+' : ''}
      {v.toFixed(2)}%
    </span>
  );
}

export default function IndustryPage() {
  const [window, setWindow] = useState<number>(20);
  const { data, error, isLoading } = useSWR<RotationResponse>(
    `/industry/rotation?window=${window}`,
    fetcher,
    { refreshInterval: 120_000 },
  );

  const rows = data?.rows ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="行业分析"
        sub={
          <>
            行业轮动榜 · {data?.asof ?? '暂无数据'} · 标准 {data?.std ?? '—'}
            {' '}· 收益为成分股等权合成的行业指数口径
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-ink-faint">排名窗口</span>
        {WINDOWS.map((w) => (
          <button
            key={w.key}
            type="button"
            onClick={() => setWindow(w.key)}
            className={`rounded px-3 py-1 text-sm transition-colors ${
              window === w.key ? 'bg-ink text-paper' : 'bg-paper text-ink-dim hover:text-ink'
            }`}
          >
            {w.label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <Loading />
      ) : error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : !rows.length ? (
        <Empty>
          暂无行业数据 —— 先同步<span className="mx-1">行业分类</span>与日线：
          <code className="bg-paper px-1">lq data reference</code>
          {' '}→<code className="ml-1 bg-paper px-1">lq data sync</code>
        </Empty>
      ) : (
        <Panel bodyClass="" title="行业轮动榜"
               meta={`按 ${window} 日收益排序 · 共 ${rows.length} 个行业`}>
          <RotationTable rows={rows} window={window} />
        </Panel>
      )}

      {data?.notes?.length ? (
        <ul className="space-y-1 text-xs text-ink-faint">
          {data.notes.map((n, i) => <li key={i}>· {n}</li>)}
        </ul>
      ) : null}
    </div>
  );
}

function RotationTable({ rows, window }: { rows: RotationRow[]; window: number }) {
  const maxAbs = Math.max(
    ...rows.map((r) => Math.abs(typeof r[`r${window}`] === 'number'
      ? (r[`r${window}`] as number) : 0)),
    1,
  );
  return (
    <div className="overflow-x-auto">
      <table className="table-dense w-full">
        <thead>
          <tr>
            <th className="pl-4 text-left">#</th>
            <th className="text-left">行业</th>
            <th className="text-left">区间收益</th>
            <th className="text-right">成员数</th>
            <th className="text-right">20 日成交额</th>
            <th className="text-right">PE 中位数</th>
            <th className="pr-4 text-right">全行业分位</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const ret = typeof r[`r${window}`] === 'number' ? (r[`r${window}`] as number) : null;
            const w = ret == null ? 0 : (Math.abs(ret) / maxAbs) * 100;
            const bg = (ret ?? 0) >= 0 ? 'rgba(195,53,43,.12)' : 'rgba(30,124,85,.12)';
            return (
              <tr key={r.industry_code} className="hover:bg-white">
                <td className="pl-4 text-xs tabular-nums text-ink-faint">{r.rank}</td>
                <td>
                  <Link href={`/industry/${encodeURIComponent(r.industry_code)}`}
                        className="font-medium hover:underline">
                    {r.industry_name}
                  </Link>
                  <span className="ml-2 font-mono text-[11px] text-ink-faint">
                    {r.industry_code}
                  </span>
                </td>
                <td>
                  <div className="relative h-5 w-40">
                    <div className="absolute top-0.5 h-4" style={{ width: `${w}%`, background: bg }} />
                    <span className="relative text-xs font-medium">
                      <Pct v={ret} />
                    </span>
                  </div>
                </td>
                <td className="text-right text-xs tabular-nums text-ink-dim">{r.n_members}</td>
                <td className="text-right text-xs tabular-nums text-ink-dim">
                  {fmtYi(r.amount_20d)}
                </td>
                <td className="text-right text-xs tabular-nums text-ink-dim">
                  {r.pe_median == null ? '—' : r.pe_median.toFixed(1)}
                </td>
                <td className="pr-4 text-right text-xs tabular-nums text-ink-faint">
                  {r.percentile.toFixed(0)}%
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
