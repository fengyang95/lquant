import Link from 'next/link';

/** 页面标题行：宋体标题 + 弱化说明 + 右侧动作区 */
export default function PageHeader({
  title,
  sub,
  actions,
}: {
  title: string;
  sub?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-[26px] font-semibold leading-none tracking-[0.08em]">{title}</h1>
        {sub ? <div className="mt-2 text-xs text-ink-faint">{sub}</div> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** 侧栏导航分组小标题（市场 / 研究 / 数据） */
export function NavGroup({ label }: { label: string }) {
  return (
    <div className="mb-1 px-4 pt-5 pb-1 text-[11px] tracking-[0.2em] text-ink-faint">{label}</div>
  );
}

export function NavLink({
  href,
  label,
  active,
}: {
  href: string;
  label: string;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      className={`relative block px-4 py-1.5 text-sm transition-colors ${
        active ? 'bg-white text-ink' : 'text-ink-dim hover:bg-white/70 hover:text-ink'
      }`}
    >
      {active && <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 bg-up" />}
      {label}
    </Link>
  );
}
