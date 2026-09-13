'use client';

import { useEffect, useState } from 'react';
import { useSWRConfig } from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { NewsFeedList } from './NewsFeedList';
import { useNewsFeed } from './useNewsFeed';
import IndustryPanel from './IndustryPanel';
import SymbolSearch from './SymbolSearch';
import { CollectBar } from './CollectBar';
import { fetchSources } from './lib';
import type { ItemFilter } from './types';

const TABS = [
  { key: 'feed', label: '全部流' },
  { key: 'industry', label: '行业' },
  { key: 'symbol', label: '个股' },
] as const;

type TabKey = (typeof TABS)[number]['key'];
type PageFilters = { source: string; keyword: string; reload: number };

/** 来源过滤下拉：来源统计并集（0 计数来源也可选），加载失败静默降级为「全部来源」 */
function SourceFilter({
  value,
  reload,
  onChange,
}: {
  value: string;
  reload: number;
  onChange: (v: string) => void;
}) {
  const [sources, setSources] = useState<{ source: string; count: number }[]>([]);

  useEffect(() => {
    let alive = true;
    fetchSources()
      .then((rows) => {
        if (alive) setSources(rows);
      })
      .catch(() => {
        if (alive) setSources([]);
      });
    return () => {
      alive = false;
    };
  }, [reload]);

  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className="input max-w-[180px]">
      <option value="">全部来源</option>
      {sources.map((s) => (
        <option key={s.source} value={s.source}>
          {s.source}（{s.count}）
        </option>
      ))}
    </select>
  );
}

/** 单一资讯流：extra 为 tab 特有过滤（industry/symbol），filters 为跨 tab 共享过滤 */
function FeedView({
  extra,
  filters,
  emptyHint,
}: {
  extra: ItemFilter;
  filters: PageFilters;
  emptyHint?: string;
}) {
  const feed = useNewsFeed({ ...filters, ...extra });
  if (feed.error) return <ErrorNote>加载失败：{feed.error}</ErrorNote>;
  return (
    <div>
      {feed.loading && feed.items.length === 0 ? <Loading /> : null}
      <NewsFeedList
        items={feed.items}
        total={feed.total}
        limit={50}
        offset={feed.offset}
        onLoadMore={feed.loadMore}
      />
      {!feed.loading && feed.items.length === 0 && emptyHint ? <Empty>{emptyHint}</Empty> : null}
    </div>
  );
}

export default function NewsPage() {
  const [tab, setTab] = useState<TabKey>('feed');
  // 来源/关键词过滤跨 tab 保留；keyword 防抖 300ms 再进查询（仿 SymbolSearch）
  const [source, setSource] = useState('');
  const [keyword, setKeyword] = useState('');
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const [symbol, setSymbol] = useState('');
  const [reload, setReload] = useState(0);
  const { mutate } = useSWRConfig();

  useEffect(() => {
    const t = setTimeout(() => setDebouncedKeyword(keyword), 300);
    return () => clearTimeout(t);
  }, [keyword]);

  const filters: PageFilters = { source, keyword: debouncedKeyword, reload };

  /** 采集完成后：资讯流靠 reload 重查，行业计数走 SWR 失效 */
  const onCollected = () => {
    setReload((n) => n + 1);
    void mutate('/news/industries');
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="资讯"
        sub="行业与个股资讯流 · 电报/新闻/社媒/研报"
        actions={<CollectBar onDone={onCollected} />}
      />

      {/* 过滤条 + tab 切换 */}
      <div className="flex flex-wrap items-center gap-3 border border-line bg-panel px-4 py-2.5">
        <div className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-3 py-1 text-sm ${
                tab === t.key ? 'bg-white text-ink' : 'text-ink-dim hover:bg-white/70'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="关键词过滤"
            className="input w-44"
          />
          <SourceFilter value={source} reload={reload} onChange={setSource} />
        </div>
      </div>

      {tab === 'feed' ? <FeedView extra={{}} filters={filters} /> : null}

      {tab === 'industry' ? <IndustryPanel filters={filters} /> : null}

      {tab === 'symbol' ? (
        <div className="space-y-4">
          <Panel title="个股">
            <SymbolSearch onSelect={setSymbol} />
            {symbol ? (
              <p className="mt-3 text-xs text-ink-faint">
                当前标的：<span className="font-mono text-ink">{symbol}</span>
                <button
                  className="ml-2 text-ink-faint hover:text-up"
                  onClick={() => setSymbol('')}
                >
                  清除
                </button>
              </p>
            ) : null}
          </Panel>
          {symbol ? (
            <FeedView extra={{ symbol }} filters={filters} />
          ) : (
            <Empty>搜索并选择一只个股查看其资讯流</Empty>
          )}
        </div>
      ) : null}
    </div>
  );
}
