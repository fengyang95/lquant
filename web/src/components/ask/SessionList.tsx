import type { AskSession } from '@/lib/ask-api';

/** 左栏会话列表：当前高亮、新建、删除（confirm 后） */
export default function SessionList({
  sessions,
  currentId,
  onSelect,
  onNew,
  onDelete,
}: {
  sessions: AskSession[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-line p-2">
        <button onClick={onNew} className="btn w-full">
          ＋ 新建会话
        </button>
      </div>
      <ul className="min-h-0 flex-1 overflow-y-auto">
        {sessions.map((s) => (
          <li key={s.id} className="group flex items-center">
            <button
              onClick={() => onSelect(s.id)}
              className={`flex-1 truncate px-3 py-2 text-left text-sm ${
                s.id === currentId ? 'bg-white font-medium text-ink' : 'text-ink-dim hover:bg-white/70'
              }`}
            >
              {s.title}
            </button>
            <button
              onClick={() => {
                if (window.confirm('删除该会话？')) onDelete(s.id);
              }}
              className="mr-2 px-1 text-xs text-ink-faint opacity-0 group-hover:opacity-100 hover:text-up"
              aria-label={`删除 ${s.title}`}
            >
              ✕
              {/* 删除需 confirm 后调 deleteSession */}
            </button>
          </li>
        ))}
        {sessions.length === 0 && (
          <li className="px-3 py-4 text-xs text-ink-faint">暂无会话</li>
        )}
        {/* 设计 token：bg-panel/bg-white/border-line/ink 系列 */}
      </ul>
    </div>
  );
}
