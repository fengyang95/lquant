'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { NavGroup, NavLink } from '@/components/PageHeader';

const GROUPS: { label: string; items: { href: string; label: string }[] }[] = [
  {
    label: '市场',
    items: [
      { href: '/dashboard', label: '大盘' },
      { href: '/security', label: '个股分析' },
      { href: '/industry', label: '行业分析' },
      { href: '/sectors', label: '板块' },
      { href: '/watchlist', label: '自选' },
      { href: '/news', label: '资讯' },
    ],
  },
  {
    label: '研究',
    items: [
      { href: '/factors', label: '因子' },
      { href: '/factors/editor', label: '因子编辑' },
      { href: '/factors/mine', label: '因子挖掘' },
      { href: '/factors/reports', label: '因子报告' },
      { href: '/fundamental', label: '基本面' },
      { href: '/backtests', label: '回测' },
      { href: '/ml', label: '模型' },
      { href: '/paper', label: '模拟盘' },
    ],
  },
  {
    label: '数据',
    items: [
      { href: '/data', label: '数据总览' },
      { href: '/sync', label: '同步' },
      { href: '/tasks', label: '任务管理' },
      { href: '/monitor', label: '监控' },
    ],
  },
];

export default function Sidebar() {
  const pathname = usePathname();
  const active = (href: string) =>
    pathname === href || (href !== '/' && pathname.startsWith(`${href}/`));

  return (
    <aside className="fixed inset-y-0 left-0 z-20 flex w-44 flex-col border-r border-line bg-paper">
      {/* 铭牌：朱砂印章 + 站名 */}
      <Link href="/dashboard" className="flex items-center gap-2.5 border-b border-line px-4 py-4">
        <span className="flex h-8 w-8 items-center justify-center bg-up font-song text-lg font-bold text-white">
          量
        </span>
        <span className="font-song text-lg font-semibold tracking-[0.12em]">lquant</span>
      </Link>

      <nav className="flex-1 overflow-y-auto pb-4">
        {GROUPS.map((g) => (
          <div key={g.label}>
            <NavGroup label={g.label} />
            {g.items.map((n) => (
              <NavLink key={n.href} href={n.href} label={n.label} active={active(n.href)} />
            ))}
          </div>
        ))}
      </nav>

      <div className="border-t border-line px-4 py-3 text-[11px] leading-relaxed text-ink-faint">
        A 股量化研究台
        <br />
        红涨 · 绿跌
      </div>
    </aside>
  );
}
