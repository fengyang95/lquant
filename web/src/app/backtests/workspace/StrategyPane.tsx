// 策略库栏 —— 工作台左栏：用户策略列表，载入/删除二次确认/新建入口（纯受控组件）
import { useState } from 'react';
import type { StrategyMeta } from './state';

type Props = {
  strategies: StrategyMeta[];
  selectedId: string | null;
  onLoad(id: string): void;
  onDelete(id: string, name: string): void;
  onNew(): void;
};

/** 工作台左栏策略库：列出用户策略，点击载入，删除需二次确认，底部新建入口。 */
export default function StrategyPane({ strategies, selectedId, onLoad, onDelete, onNew }: Props) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  return (
    <div className="flex flex-col">
      {strategies.length === 0 ? (
        <div className="p-4 text-xs text-ink-faint">策略库还是空的 —— 点「新建」写一个</div>
      ) : (
        <ul className="max-h-[480px] overflow-y-auto">
          {strategies.map((s) => (
            <li key={s.id} className="group relative">
              <button
                onClick={() => s.id && onLoad(s.id)}
                className={`block w-full px-4 py-2 pr-16 text-left text-sm transition-colors ${
                  s.id === selectedId ? 'bg-white text-ink' : 'text-ink-dim hover:bg-white/70'
                }`}
              >
                <span className="block truncate font-medium">{s.name}</span>
                {s.description && (
                  <span className="block truncate text-xs text-ink-faint">{s.description}</span>
                )}
              </button>
              {confirmingId === s.id ? (
                <div className="absolute inset-y-0 right-1 flex items-center gap-1 text-xs">
                  <span className="text-ink-dim">确认删除？</span>
                  <button
                    onClick={() => {
                      if (s.id) onDelete(s.id, s.name);
                      setConfirmingId(null);
                    }}
                    className="rounded-[2px] border border-ink bg-ink px-1.5 py-0.5 text-paper"
                  >
                    确定
                  </button>
                  <button
                    onClick={() => setConfirmingId(null)}
                    className="rounded border border-line px-1.5 py-0.5 text-ink-dim"
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmingId(s.id ?? null)}
                  className="absolute inset-y-0 right-1 my-auto hidden h-6 items-center rounded px-1.5 text-xs text-ink-faint hover:text-ink group-hover:flex"
                >
                  删除
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="border-t border-line p-2">
        <button
          onClick={onNew}
          className="w-full rounded border border-line py-1.5 text-sm text-ink-dim transition-colors hover:bg-white/70 hover:text-ink"
        >
          + 新建策略
        </button>
      </div>
    </div>
  );
}
