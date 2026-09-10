'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { AgentEventMsg, AskMessage, AskSession } from '@/lib/ask-api';
import { connectAskEvents, getMessages, reduceMessages, sendMessage } from '@/lib/ask-api';
import Message from './Message';

/** 右栏对话窗口：历史加载、事件流订阅、乐观发送、错误重试 */
export default function ChatWindow({ session }: { session: AskSession }) {
  const [msgs, setMsgs] = useState<AskMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastSentRef = useRef('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const sid = session.id;

  // 选中会话：加载历史 → 订阅事件流；切换会话取消旧订阅
  useEffect(() => {
    let alive = true;
    let cancel: (() => void) | null = null;
    setMsgs([]);
    setError(null);
    setPending(false);
    getMessages(sid)
      .then((list) => {
        if (alive) setMsgs(list);
      })
      .catch(() => {
        if (alive) setError('历史消息加载失败');
      });
    cancel = connectAskEvents(sid, (ev: AgentEventMsg) => {
      if (ev.type === 'done') {
        // done 只走 onEvent（ask-api 已知缺陷）：拉落库消息对账替换，不依赖 onDone
        setPending(false);
        getMessages(sid)
          .then((list) => {
            if (alive) setMsgs(list);
          })
          .catch(() => {});
      } else if (ev.type === 'error') {
        setError(ev.message ?? '生成失败，请重试');
        setPending(false);
      } else {
        setMsgs((prev) => reduceMessages(prev, ev));
        if (ev.type !== 'tool_result') setPending(true);
      }
    });
    return () => {
      alive = false;
      cancel?.();
    };
  }, [sid]);

  // 新消息自动滚底
  useEffect(() => {
    if (typeof bottomRef.current?.scrollIntoView === 'function') {
      bottomRef.current.scrollIntoView();
    }
  }, [msgs]);

  const send = useCallback(async () => {
    const content = draft.trim();
    if (!content || pending) return;
    setDraft('');
    setPending(true);
    setError(null);
    lastSentRef.current = content;
    const local: AskMessage = {
      id: `local-${Date.now()}`,
      session_id: sid,
      role: 'user',
      content,
      tool_calls: [],
      created_at: new Date().toISOString(),
    };
    setMsgs((prev) => [...prev, local]);
    try {
      await sendMessage(sid, content);
    } catch (e) {
      setPending(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [draft, pending, sid]);

  const retry = useCallback(async () => {
    // 重发原文本
    const text = lastSentRef.current;
    if (!text || pending) return;
    setPending(true);
    setError(null);
    const local: AskMessage = {
      id: `local-${Date.now()}`,
      session_id: sid,
      role: 'user',
      content: text,
      tool_calls: [],
      created_at: new Date().toISOString(),
    };
    setMsgs((prev) => [...prev, local]);
    try {
      await sendMessage(sid, text);
    } catch (e) {
      setPending(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pending, sid]);

  return (
    <div className="flex h-full flex-col">
      {/* 消息流 */}
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {msgs.length === 0 && !pending && !error ? (
          <div className="border border-dashed border-line-strong bg-panel px-6 py-12 text-center text-sm text-ink-faint">
            还没有消息，输入问题开始对话
          </div>
        ) : null}
        {msgs.map((m) => (
          <Message key={m.id} message={m} />
        ))}
        {pending ? <div className="px-1 text-xs text-ink-faint">正在生成…</div> : null}
        {error ? (
          <div className="border-l-2 border-up bg-panel px-4 py-3 text-sm text-up">
            {error}
            <button onClick={() => void retry()} className="btn ml-3 px-2 py-0.5 text-xs">
              重试
            </button>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>
      {/* 输入区：Enter 发送、Shift+Enter 换行 */}
      <div className="border-t border-line p-3">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          rows={2}
          className="input w-full resize-none"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
      </div>
    </div>
  );
}
