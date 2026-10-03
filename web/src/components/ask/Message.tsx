import type { AskMessage } from '@/lib/ask-api';

import Markdown from '../Markdown';

/** 单条消息：user 右对齐、assistant 左对齐；tool_calls 渲染为折叠 details */
export default function Message({ message }: { message: AskMessage }) {
  const isUser = message.role === 'user';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className="max-w-[80%]">
        {message.tool_calls.length > 0 && (
          <details className="mb-1.5 border border-line bg-panel text-xs text-ink-faint">
            <summary className="cursor-pointer select-none px-2.5 py-1.5">
              {message.toolDone
                ? `${message.tool_calls.map((t) => t.name ?? 'tool').join('、')} ✓`
                : `正在查询 ${message.tool_calls.map((t) => t.name ?? 'tool').join('、')}…`}
            </summary>
            <div className="border-t border-line px-2.5 py-1.5 font-mono">
              {message.tool_calls.map((t, i) => (
                <div key={i}>
                  {t.name}
                  {t.args ? `(${JSON.stringify(t.args)})` : ''}
                </div>
              ))}
            </div>
          </details>
        )}
        {/* 用户消息保持纯文本：输入里的 `*`、`#` 不该被当成格式。
            助手回答走 Markdown（表格 / 列表 / 代码块），见 components/Markdown.tsx。 */}
        {message.content ? (
          isUser ? (
            <div className="whitespace-pre-wrap rounded-sm bg-up/10 px-3 py-2 text-sm leading-relaxed text-ink">
              {message.content}
            </div>
          ) : (
            <div className="rounded-sm bg-panel px-3 py-2">
              <Markdown content={message.content} />
            </div>
          )
        ) : (
          <div className="px-1 text-xs text-ink-faint">正在生成…</div>
        )}
      </div>
    </div>
  );
}
