/** 空态 / 错误态：纸面上的说明文字，空态给出可执行的下一步命令 */
export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="border border-dashed border-line-strong bg-panel px-6 py-12 text-center text-sm text-ink-faint">
      {children}
    </div>
  );
}

export function Loading({ children = '加载中…' }: { children?: React.ReactNode }) {
  return <div className="px-6 py-16 text-center text-sm text-ink-faint">{children}</div>;
}

export function ErrorNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="border-l-2 border-up bg-panel px-4 py-3 text-sm text-up">{children}</div>
  );
}

/** 操作反馈条（成功 / 失败共用，文案前缀区分） */
export function Msg({ text }: { text: string }) {
  if (!text) return null;
  const ok = text.startsWith('✓');
  return (
    <div className={`border-l-2 px-4 py-2.5 text-sm ${ok ? 'border-down bg-panel text-ink' : 'border-up bg-panel text-up'}`}>
      {text}
    </div>
  );
}
