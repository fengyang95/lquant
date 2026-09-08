/**
 * 面板：发丝线边框、无投影、标题条下压一根线。
 * 用「分栏 + 竖分隔线」代替一排同款小卡片。
 */
export function Panel({
  title,
  meta,
  actions,
  children,
  className = '',
  bodyClass = 'p-4',
}: {
  title?: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={`border border-line bg-panel ${className}`}>
      {title != null && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2">
          <div className="flex items-baseline gap-2">
            <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
            {meta ? <span className="text-xs text-ink-faint">{meta}</span> : null}
          </div>
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

/** 指标块：弱标签 + 宋体大数字。用于并排指标条。 */
export function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  tone?: string;
  hint?: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-ink-faint">{label}</div>
      <div className={`mt-0.5 font-song text-[22px] font-semibold leading-tight tabular-nums ${tone ?? 'text-ink'}`}>
        {value}
      </div>
      {hint ? <div className="mt-0.5 text-xs text-ink-faint">{hint}</div> : null}
    </div>
  );
}
