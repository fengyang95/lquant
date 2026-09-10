'use client';

import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { fetcherData } from '@/lib/api';
import { NewsFeedList } from './NewsFeedList';
import { fetchIndustries, fetchItems } from './lib';
import type { IndustryStat, ItemFilter, NewsItemDTO } from './types';

/** 左行业列表（计数 + 中文名）+ 右资讯流：点击行业 → 右侧按 industry=code 查询。 */
export function IndustryPanel({ filters = {} }: { filters?: ItemFilter }) {
  const { data: industries, error: indError, isLoading: indLoading } = useSWR<IndustryStat[]>(
    '/news/industries',
    fetcherData,
    { revalidateOnFocus: false },
  );

  const [selected, setSelected] = useState<string | null>(null);
  const gen = useRef(0); // 查询代际：selected/filters 变化 +1，使在途 loadMore 落地时自弃
  const [page, setPage] = useState<{
    items: NewsItemDTO[];
    total: number;
    offset: number;
    loading: boolean;
    error: string;
  }>({ items: [], total: 0, offset: 0, loading: false, error: '' });

  // 选中行业或共享过滤条件变化 → 重查第一页
  useEffect(() => {
    if (!selected) return;
    gen.current += 1;
    let alive = true;
    setPage((p) => ({ ...p, loading: true, error: '' }));
    fetchItems({ ...filters, industry: selected, limit: 50, offset: 0 })
      .then((res) => {
        if (!alive) return;
        setPage({ items: res.items, total: res.total, offset: 0, loading: false, error: '' });
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setPage((p) => ({ ...p, loading: false, error: e instanceof Error ? e.message : String(e) }));
      });
    return () => {
      alive = false;
    };
  }, [selected, JSON.stringify(filters)]);

  const loadMore = () => {
    if (!selected) return;
    const id = gen.current; // 落地前比对代际：期间已切换行业/过滤则丢弃本次追加
    setPage((p) => ({ ...p, loading: true }));
    fetchItems({ ...filters, industry: selected, limit: 50, offset: page.items.length })
      .then((res) => {
        if (gen.current !== id) return;
        setPage((p) => ({ ...p, items: [...p.items, ...res.items], total: res.total, offset: p.items.length, loading: false }));
      })
      .catch((e: unknown) => {
        if (gen.current !== id) return;
        setPage((p) => ({ ...p, loading: false, error: e instanceof Error ? e.message : String(e) }));
      });
  };

  return (
    <div className="grid grid-cols-[220px_1fr] gap-4">
      {/* 左：行业清单 */}
      <div className="border border-line bg-panel">
        <div className="border-b border-line px-3 py-2 text-[13px] font-semibold text-ink">行业</div>
        {indLoading ? <Loading /> : null}
        {indError ? <ErrorNote>行业统计加载失败：{String(indError)}</ErrorNote> : null}
        {!indLoading && !indError && industries && industries.length === 0 ? (
          <Empty>暂无行业数据</Empty>
        ) : null}
        <ul>
          {(industries ?? []).map((ind) => (
            <li key={ind.industry_code}>
              <button
                onClick={() => setSelected(ind.industry_code)}
                className={`flex w-full items-center justify-between px-3 py-1.5 text-left text-sm ${
                  selected === ind.industry_code ? 'bg-white text-ink' : 'text-ink-dim hover:bg-white/70'
                }`}
              >
                <span className="truncate">{ind.industry_name || ind.industry_code}</span>
                <span className="ml-2 shrink-0 font-mono text-xs tabular-nums text-ink-faint">{ind.count}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* 右：选中行业的资讯流 */}
      <div className="border border-line bg-panel">
        <div className="border-b border-line px-3 py-2 text-[13px] font-semibold text-ink">
          {selected ? `${industries?.find((i) => i.industry_code === selected)?.industry_name || selected} 资讯` : '资讯'}
          {selected ? <span className="ml-2 text-xs font-normal text-ink-faint">共 {page.total} 条</span> : null}
        </div>
        {!selected ? (
          <Empty>← 左侧选择一个行业查看资讯流</Empty>
        ) : page.error ? (
          <ErrorNote>加载失败：{page.error}</ErrorNote>
        ) : (
          <NewsFeedList
            items={page.items}
            total={page.total}
            limit={50}
            offset={page.offset}
            onLoadMore={page.offset + page.items.length < page.total ? loadMore : undefined}
          />
        )}
        {page.loading ? <Loading>加载中…</Loading> : null}
      </div>
    </div>
  );
}

export default IndustryPanel;
