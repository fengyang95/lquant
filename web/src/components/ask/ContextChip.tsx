import Link from 'next/link';

/** 会话上下文徽标：context.symbol 存在时显示，点击跳个股页 */
export default function ContextChip({ symbol }: { symbol: string }) {
  return (
    <Link
      href={`/security/${encodeURIComponent(symbol)}`}
      className="inline-flex items-center gap-1 border border-line bg-panel px-2 py-0.5 font-mono text-xs text-ink-dim hover:text-ink"
    >
      {symbol}
      <span aria-hidden>↗</span>
      {/* 点击跳 /security/{symbol} */}
    </Link>
  );
}
