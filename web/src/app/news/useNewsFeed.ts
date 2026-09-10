'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchItems } from './lib';
import type { ItemFilter, NewsItemDTO } from './types';

/** /news/items 分页流：params 变化自动重查，loadMore 追加下一页。 */
export function useNewsFeed(params: ItemFilter) {
  const limit = params.limit ?? 50;
  const rest = JSON.stringify({ ...params, limit: undefined, offset: undefined });
  const [items, setItems] = useState<NewsItemDTO[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const seq = useRef(0);

  useEffect(() => {
    const id = ++seq.current;
    setLoading(true);
    setError('');
    fetchItems({ ...JSON.parse(rest), limit, offset: 0 })
      .then((page) => {
        if (seq.current !== id) return; // 过滤条件已变，丢弃过期响应
        setItems(page.items);
        setTotal(page.total);
        setOffset(0);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (seq.current !== id) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
  }, [rest, limit]);

  const loadMore = useCallback(() => {
    setLoading(true);
    fetchItems({ ...JSON.parse(rest), limit, offset: items.length })
      .then((page) => {
        setItems((prev) => [...prev, ...page.items]); // 追加去重由 news_id key 兜底
        setOffset(items.length);
        setTotal(page.total);
        setLoading(false);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
  }, [rest, limit, items.length]);

  return { items, total, offset, loading, error, loadMore };
}
