'use client';

/**
 * 打开已有因子：列出因子库，选中后把它的 DSL 表达式反解析成画布。
 *
 * 没有表达式（先存后编译的占位因子）的条目不可选 —— 打开只会得到空画布，
 * 不如直接说明原因。
 */
import { useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';

import { ErrorNote, Loading } from '@/components/States';
import { fetcher } from '@/lib/api';

export type FactorListItem = {
  name: string;
  expression: string;
  description: string;
  source: string;
  category: string;
  ic_neutral: number | null;
};

const SOURCE_LABEL: Record<string, string> = {
  qlib: '内置',
  yaml: '配置',
  manual: '手建',
  mined: '挖掘',
};

export default function OpenFactorDialog({
  open,
  onClose,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (factor: FactorListItem) => void;
}) {
  const [query, setQuery] = useState('');
  const { data, error, isLoading } = useSWR<FactorListItem[]>(
    open ? '/factors?limit=300' : null,
    fetcher,
  );

  useEffect(() => {
    if (!open) setQuery('');
  }, [open]);

  const items = useMemo(() => {
    const list = data ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (f) =>
        f.name.toLowerCase().includes(q) ||
        (f.description ?? '').toLowerCase().includes(q) ||
        (f.category ?? '').toLowerCase().includes(q),
    );
  }, [data, query]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center bg-ink/20 p-6"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="mt-12 flex max-h-[70vh] w-full max-w-2xl flex-col border border-line bg-panel shadow-lift"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-label="打开已有因子"
      >
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2">
          <h2 className="text-[13px] font-semibold">打开已有因子</h2>
          <button type="button" className="btn btn-sm" onClick={onClose}>
            关闭
          </button>
        </header>

        <div className="border-b border-line px-4 py-2">
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按因子名 / 说明 / 分类搜索"
            className="input w-full"
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? <Loading /> : null}
          {error ? (
            <div className="p-4">
              <ErrorNote>
                因子列表读取失败：{error instanceof Error ? error.message : String(error)}
              </ErrorNote>
            </div>
          ) : null}
          {data && items.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-ink-faint">
              没有匹配的因子。先去「因子」页注册，或用「内置因子」灌入。
            </p>
          ) : null}
          <ul>
            {items.map((factor) => {
              const openable = Boolean(factor.expression?.trim());
              return (
                <li key={factor.name} className="border-b border-line last:border-b-0">
                  <button
                    type="button"
                    disabled={!openable}
                    onClick={() => onPick(factor)}
                    className="block w-full px-4 py-2.5 text-left transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="font-mono text-sm text-ink">{factor.name}</span>
                      <span className="shrink-0 text-[11px] text-ink-faint">
                        {SOURCE_LABEL[factor.source] ?? factor.source} · {factor.category}
                        {factor.ic_neutral != null ? ` · IC ${factor.ic_neutral.toFixed(3)}` : ''}
                      </span>
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[11px] text-ink-dim">
                      {openable ? factor.expression : '（无表达式，画布无法打开）'}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}
