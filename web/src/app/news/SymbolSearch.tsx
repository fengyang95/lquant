'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { fetcher } from '@/lib/api';

type SecRow = { symbol: string; name: string | null; sec_type: string };

/** 个股搜索：防抖 300ms 走 /data/securities 搜索端点，点击建议回调 onSelect。 */
export function SymbolSearch({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [q, setQ] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const { data: suggestions } = useSWR<SecRow[]>(
    debouncedQ.trim().length >= 2
      ? `/data/securities?q=${encodeURIComponent(debouncedQ.trim())}&limit=8&sec_type=stock`
      : null,
    fetcher,
  );

  return (
    <div>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="输入代码或名称搜索个股资讯，如 600519 / 贵州茅台"
        className="input w-full max-w-md"
      />
      {suggestions && suggestions.length > 0 ? (
        <div className="mt-2 max-w-md divide-y divide-line border border-line bg-white">
          {suggestions.map((s) => (
            <button
              key={s.symbol}
              onClick={() => {
                onSelect(s.symbol);
                setQ('');
                setDebouncedQ('');
              }}
              className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-paper"
            >
              <span>
                <span className="font-medium">{s.name || '—'}</span>
                <span className="ml-2 font-mono text-xs text-ink-faint">{s.symbol}</span>
              </span>
              <span className="text-xs text-ink-faint">查看资讯</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default SymbolSearch;
