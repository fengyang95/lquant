'use client';

/**
 * 个股分析入口页：输入一个股票代码 → 进入该标的的多角度分析。
 *
 * 为什么单独做一个入口页：`/security/{symbol}` 是详情页，只能从自选/看板点进来，
 * **没有一个「我就想看这只票」的输入口**。这一页补的就是这个动作。
 */
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { Pct, fmtNum } from '@/components/QuoteTable';
import { fetcher } from '@/lib/api';

type SecRow = { symbol: string; name: string | null; sec_type: string | null };

type WatchRow = {
  symbol: string;
  name: string | null;
  close: number | null;
  trade_date: string | null;
  change_pct: number | null;
};

type AngleRow = { id: string; label: string; weight: number; desc: string };

/** 裸 6 位或带交易所后缀都接受（后端会做统一归一） */
const LOOKS_LIKE_CODE = /^\d{6}(\.(SH|SZ|BJ))?$/i;

export default function SecurityIndexPage() {
  const router = useRouter();
  const [q, setQ] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');

  // 输入防抖 300ms 再发搜索（与自选页同口径）
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const { data: suggestions } = useSWR<SecRow[]>(
    debouncedQ.trim().length >= 2
      ? `/data/securities?q=${encodeURIComponent(debouncedQ.trim())}&limit=8`
      : null,
    fetcher,
  );
  const { data: watch, isLoading, error } = useSWR<WatchRow[]>('/watchlist', fetcher);
  const { data: angleMeta } = useSWR<{ angles: AngleRow[] }>('/security/angles', fetcher);

  const go = (symbol: string) => {
    const s = symbol.trim();
    if (!s) return;
    router.push(`/security/${encodeURIComponent(s)}`);
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="个股分析"
        sub="输入一个股票代码，从技术面 · 基本面 · 估值 · 资金面 · 相对强度 · 消息面多角度分析"
      />

      <Panel title="输入股票代码" meta="支持裸 6 位，如 600519 / 000001">
        <form
          onSubmit={(e) => { e.preventDefault(); go(q); }}
          className="flex flex-wrap items-center gap-2"
        >
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="输入代码或名称搜索，如 600519 / 贵州茅台"
            aria-label="股票代码"
            className="input w-full max-w-md"
          />
          <button type="submit" className="btn-primary" disabled={!q.trim()}>
            开始分析
          </button>
        </form>
        {q.trim() && !LOOKS_LIKE_CODE.test(q.trim()) && (!suggestions || suggestions.length === 0) && (
          <p className="mt-2 text-xs text-ink-faint">
            代码形如 6 位数字（可带 .SH/.SZ）；也可按名称搜索后从下方结果进入。
          </p>
        )}

        {suggestions && suggestions.length > 0 && (
          <div className="mt-3 max-w-md divide-y divide-line border border-line bg-white">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                type="button"
                onClick={() => go(s.symbol)}
                className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-paper"
              >
                <span>
                  <span className="font-medium">{s.name || '—'}</span>
                  <span className="ml-2 font-mono text-xs text-ink-faint">{s.symbol}</span>
                </span>
                <span className="text-xs text-ink-faint">分析 →</span>
              </button>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="分析覆盖的角度" meta="每个角度取不到数会明确标注，不会用默认值填充">
        {!angleMeta?.angles?.length ? (
          <Empty>角度注册表不可用（后端 /api/security/angles 未响应）</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
            {angleMeta.angles.map((a) => (
              <div key={a.id} className="border-b border-line py-2">
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-medium text-ink">{a.label}</span>
                  <span className="text-[11px] text-ink-faint">
                    {a.weight > 0 ? `权重 ${(a.weight * 100).toFixed(0)}%` : '不参与评分'}
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] text-ink-faint">{a.desc}</div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="从自选进入" meta={watch?.length ? `${watch.length} 只` : undefined}>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <ErrorNote>加载失败：{String(error)}</ErrorNote>
        ) : !watch?.length ? (
          <Empty>
            自选为空 —— 先去 <Link href="/watchlist" className="text-indigo hover:underline">自选页</Link> 添加标的
          </Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">标的</th>
                <th className="text-right">最新价</th>
                <th className="text-right">涨跌幅</th>
                <th className="text-right">数据日期</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {watch.map((w) => (
                <tr key={w.symbol} className="hover:bg-white">
                  <td>
                    <span className="font-medium">{w.name || '—'}</span>
                    <span className="ml-2 font-mono text-xs text-ink-faint">{w.symbol}</span>
                  </td>
                  <td className="text-right tabular-nums">{fmtNum(w.close)}</td>
                  <td className="text-right"><Pct value={w.change_pct} /></td>
                  <td className="text-right text-xs text-ink-faint">{w.trade_date ?? '—'}</td>
                  <td className="text-right">
                    <Link href={`/security/${w.symbol}`} className="btn text-xs">分析</Link>
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
