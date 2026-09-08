import Link from 'next/link';

const NAV = [
  { href: '/dashboard', label: '大盘看板' },
  { href: '/sectors', label: '板块' },
  { href: '/watchlist', label: '自选' },
  { href: '/factors', label: '因子研究' },
  { href: '/backtests', label: '回测' },
  { href: '/paper', label: '模拟盘' },
  { href: '/data', label: '数据' },
  { href: '/sync', label: '同步' },
];

export const metadata = { title: 'lquant', description: 'A股量化研究平台' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-neutral-50 text-neutral-900 antialiased">
        <header className="border-b bg-white">
          <div className="mx-auto flex max-w-7xl items-center gap-1 px-6 py-3">
            <Link href="/" className="mr-6 text-lg font-bold tracking-tight">
              l<span className="text-red-600">quant</span>
            </Link>
            <nav className="flex gap-1">
              {NAV.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  className="rounded-md px-3 py-1.5 text-sm text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900"
                >
                  {n.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
