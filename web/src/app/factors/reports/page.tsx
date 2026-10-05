'use client';

/**
 * 因子报告中心 —— 全部评价/合成/挖掘报告列表，点击新窗口打开 HTML 报告。
 *
 * 三个此前会误导读者的点已修（评审 R15）：
 * 1. 每份报告标出**生成器版本**与**生成时间** —— 报告文件名不含版本，
 *    没有这两个字段时读者无从分辨「这份是修复前还是修复后的口径」。
 * 2. 版本对不上当前生成器（或缺失版本号 = 修复前产物）标「旧口径」，
 *    并给出汇总提示与「只看当前口径」开关。
 * 3. 按来源分组（单因子评价 / 合成因子 / CLI 批量），不再一坨平铺。
 */
import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, Loading } from '@/components/States';
import { get } from '@/lib/api';

type ReportRow = {
  name: string;
  url: string;
  size_kb: number;
  generator_version: string | null;
  generated_at: string;
  current_version: string;
  stale: boolean;
};

const GROUP_ORDER = ['单因子评价', '合成因子', 'CLI 批量'] as const;

function groupOf(name: string): (typeof GROUP_ORDER)[number] {
  if (name.startsWith('syn_')) return '合成因子';
  if (name.startsWith('factor_')) return 'CLI 批量';
  return '单因子评价';
}

export default function FactorReportsPage() {
  const { data, isLoading } = useSWR<ReportRow[]>('/factors/reports', get);
  const [q, setQ] = useState('');
  const [onlyCurrent, setOnlyCurrent] = useState(false);

  const all = data ?? [];
  const kw = q.trim().toLowerCase();
  const shown = all.filter(
    (r) => (!kw || r.name.toLowerCase().includes(kw)) && (!onlyCurrent || !r.stale),
  );
  const staleCount = all.filter((r) => r.stale).length;
  const currentVersion = all[0]?.current_version ?? '';

  return (
    <div className="space-y-5">
      <PageHeader
        title="因子报告"
        sub={`评价 · 合成 · 全部 HTML 报告汇总 · 当前报告口径 v${currentVersion}`}
      />
      {staleCount > 0 ? (
        <div className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          有 {staleCount} 份报告是<b>旧口径</b>
          （生成器版本与当前代码不一致，或生成于版本号引入之前）。
          旧报告不会自动失效 —— 结论可能已被修复，请重跑评价后再引用。
        </div>
      ) : null}
      <Panel title="报告列表" meta={`共 ${shown.length} 份`}>
        <div className="mb-3 flex items-center gap-4">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按报告名过滤，如 pct_change_20 或 syn_"
            className="input w-72 py-1 text-xs"
          />
          <label className="flex items-center gap-1 text-xs text-ink-faint">
            <input
              type="checkbox"
              checked={onlyCurrent}
              onChange={(e) => setOnlyCurrent(e.target.checked)}
            />
            只看当前口径
          </label>
        </div>
        {isLoading ? (
          <Loading />
        ) : shown.length === 0 ? (
          <Empty>暂无报告 —— 到因子页跑一次评价或合成即生成</Empty>
        ) : (
          <div className="space-y-4">
            {GROUP_ORDER.filter((g) => shown.some((r) => groupOf(r.name) === g)).map((g) => {
              const rows = shown.filter((r) => groupOf(r.name) === g);
              return (
                <div key={g}>
                  <div className="mb-1 text-xs font-semibold text-ink-faint">
                    {g}（{rows.length}）
                  </div>
                  <ul className="divide-y divide-line text-sm">
                    {rows.map((r) => (
                      <li key={r.name} className="flex items-center justify-between py-2">
                        <a
                          href={r.url}
                          target="_blank"
                          className="font-mono text-indigo hover:underline"
                          rel="noreferrer"
                        >
                          {r.name} ↗
                        </a>
                        <span className="flex items-center gap-2 text-xs text-ink-faint">
                          {r.stale ? (
                            <span
                              className="rounded bg-amber-100 px-2 py-0.5 text-amber-800"
                              title="生成器版本与当前代码不一致（或缺失）—— 报告口径可能已变"
                            >
                              旧口径
                            </span>
                          ) : null}
                          <span>{r.generated_at}</span>
                          <span>{r.generator_version ? `v${r.generator_version}` : 'v?'}</span>
                          <span>{r.size_kb} KB</span>
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
