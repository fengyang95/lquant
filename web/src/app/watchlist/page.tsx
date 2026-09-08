'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
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

  if (isLoading) return <div className="py-20 text-center text-neutral-400">加载中…</div>;
  if (error) return <div className="py-20 text-center text-red-500">加载失败：{String(error)}</div>;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">自选股</h1>

      {/* 添加：搜索标的名/代码 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">添加自选</div>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="输入代码或名称搜索，如 600519 / 贵州茅台"
          className="w-full max-w-md rounded-md border px-3 py-2 text-sm"
        />
        {msg && <div className="mt-2 text-sm">{msg}</div>}
        {suggestions && suggestions.length > 0 && (
          <div className="mt-2 max-w-md divide-y rounded-md border">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                onClick={() => add(s.symbol)}
                disabled={busy !== ''}
                className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-neutral-50 disabled:opacity-40"
              >
                <span>
                  <span className="font-medium">{s.name || '—'}</span>
                  <span className="ml-2 font-mono text-xs text-neutral-400">{s.symbol}</span>
                </span>
                <span className="text-xs text-neutral-400">
                  {busy === s.symbol ? '添加中…' : '点击添加'}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 列表：后端已带最近收盘与涨跌幅，首屏一次请求 */}
      <div className="rounded-xl border bg-white p-4">
        {!rows?.length ? (
          <div className="py-10 text-center text-sm text-neutral-400">
            还没有自选 —— 上方搜索添加第一只
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">标的</th>
                <th className="text-right font-normal">最新价</th>
                <th className="text-right font-normal">涨跌幅</th>
                <th className="text-right font-normal">数据日</th>
                <th className="text-right font-normal">备注</th>
                <th className="text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.symbol} className="border-b border-neutral-50 hover:bg-neutral-50/60">
                  <td className="py-2">
                    <a href={`/security/${r.symbol}`} className="hover:underline">
                      <span className="font-medium">{r.name || r.symbol}</span>
                      <span className="ml-1.5 font-mono text-xs text-neutral-400">{r.symbol}</span>
                    </a>
                  </td>
                  <td className="text-right tabular-nums">{fmtNum(r.close)}</td>
                  <td className="text-right"><Pct value={r.change_pct} /></td>
                  <td className="text-right tabular-nums text-neutral-400">{r.trade_date ?? '—'}</td>
                  <td className="text-right text-neutral-400">{r.note || '—'}</td>
                  <td className="text-right">
                    <button
                      onClick={() => remove(r.symbol)}
                      disabled={busy !== ''}
                      className="text-xs text-neutral-400 hover:text-red-600 disabled:opacity-40"
                    >
                      {busy === r.symbol ? '删除中…' : '移除'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
