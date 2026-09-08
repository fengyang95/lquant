'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
import { Pct, fmtNum } from '@/components/QuoteTable';
import { del, fetcher, post } from '@/lib/api';

type WatchRow = {
  symbol: string;
  name: string | null;
  note: string | null;
  added_at: string;
  close: number | null;
  trade_date: string | null;
  change_pct: number | null;
};

type SecRow = { symbol: string; name: string | null; sec_type: string };

export default function WatchlistPage() {
  const { data: rows, error, isLoading, mutate } = useSWR<WatchRow[]>('/watchlist', fetcher);
  const [q, setQ] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');

  // 输入防抖 300ms 后再发搜索请求
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const { data: suggestions } = useSWR<SecRow[]>(
    debouncedQ.trim().length >= 2 ? `/data/securities?q=${encodeURIComponent(debouncedQ.trim())}&limit=8` : null,
    fetcher,
  );

  async function add(symbol: string) {
    setBusy(symbol);
    setMsg('');
    try {
      await post('/watchlist', { symbol });
      setMsg(`✓ 已加入 ${symbol}`);
      setQ('');
      setDebouncedQ('');
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function remove(symbol: string) {
    setBusy(symbol);
    setMsg('');
    try {
      await del(`/watchlist/${symbol}`);
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  if (isLoading) return <Loading />;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;

  return (
    <div className="space-y-5">
      <PageHeader
        title="自选"
        sub={`跟踪标的清单${rows?.length ? ` · ${rows.length} 只` : ''}`}
      />

      {msg ? <Msg text={msg} /> : null}

      {/* 添加：搜索标的名/代码 */}
      <Panel title="添加自选">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="输入代码或名称搜索，如 600519 / 贵州茅台"
          className="input w-full max-w-md"
        />
        {suggestions && suggestions.length > 0 && (
          <div className="mt-3 max-w-md divide-y divide-line border border-line bg-white">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                onClick={() => add(s.symbol)}
                disabled={busy !== ''}
                className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-paper disabled:opacity-40"
              >
                <span>
                  <span className="font-medium">{s.name || '—'}</span>
                  <span className="ml-2 font-mono text-xs text-ink-faint">{s.symbol}</span>
                </span>
                <span className="text-xs text-ink-faint">
                  {busy === s.symbol ? '添加中…' : '点击添加'}
                </span>
              </button>
            ))}
          </div>
        )}
      </Panel>

      {/* 列表：后端已带最近收盘与涨跌幅，首屏一次请求 */}
      <Panel title="清单" bodyClass="">
        {!rows?.length ? (
          <Empty>还没有自选 —— 上方搜索添加第一只</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="pl-4 text-left">标的</th>
                <th className="text-right">最新价</th>
                <th className="text-right">涨跌幅</th>
                <th className="text-right">数据日</th>
                <th className="text-right">备注</th>
                <th className="pr-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.symbol} className="hover:bg-white">
                  <td className="py-2 pl-4">
                    <a href={`/security/${r.symbol}`} className="hover:underline">
                      <span className="font-medium">{r.name || r.symbol}</span>
                      <span className="ml-1.5 font-mono text-xs text-ink-faint">{r.symbol}</span>
                    </a>
                  </td>
                  <td className="text-right">{fmtNum(r.close)}</td>
                  <td className="text-right"><Pct value={r.change_pct} /></td>
                  <td className="text-right tabular-nums text-ink-faint">{r.trade_date ?? '—'}</td>
                  <td className="text-right text-ink-faint">{r.note || '—'}</td>
                  <td className="pr-4 text-right">
                    <button
                      onClick={() => remove(r.symbol)}
                      disabled={busy !== ''}
                      className="text-xs text-ink-faint hover:text-up disabled:opacity-40"
                    >
                      {busy === r.symbol ? '移除中…' : '移除'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
