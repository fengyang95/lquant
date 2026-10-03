'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { AgentEventMsg, AskMessage, AskSession } from '@/lib/ask-api';
import {
  cancelSession,
  connectAskEvents,
  getMessages,
  reduceMessages,
  regenerateSession,
  sendMessage,
} from '@/lib/ask-api';
import { PROVIDER_LABELS } from '@/lib/agent-api';
import type { RunTrace } from '@/lib/ask-stream';
import { applyTraceEvent, newRunTrace } from '@/lib/ask-stream';
import { fmtDuration } from '@/lib/format';
import ForkDialog from './ForkDialog';
import Message from './Message';
import RunTraceView from './RunTraceView';
import SessionConfigDialog from './SessionConfigDialog';

/** 会进过程轨的事件类型。assistant_delta 只进正文，不进过程。 */
const TRACE_EVENTS = new Set([
  'thinking', 'tool_call', 'tool_result', 'system', 'done', 'error',
]);

/** 会话头：能力集只读展示 + 「调整能力 / 重新生成 / 换后端」入口。
 *  provider 不在这里改（换了会续到别的 CLI 会话），skill / MCP 可以。 */
function SessionHeader({
  session,
  running,
  canRegenerate,
  onEdit,
  onRegenerate,
  onFork,
}: {
  session: AskSession;
  running: boolean;
  canRegenerate: boolean;
  onEdit: () => void;
  onRegenerate: () => void;
  onFork: () => void;
}) {
  const cfg = session.agent_config ?? {};
  const provider = cfg.provider ?? '';
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-1.5 text-xs">
      <span className="tag tag-on">{PROVIDER_LABELS[provider] ?? (provider || '默认后端')}</span>
      <CountChip label="skill" names={cfg.skills} />
      <CountChip label="MCP 工具" names={cfg.mcp_tools} />
      {cfg.timeout_seconds ? <span className="tag">超时 {cfg.timeout_seconds}s</span> : null}
      {cfg.skip_permissions === false ? <span className="tag">非全自主</span> : null}
      <div className="ml-auto flex items-center gap-2">
        {/* 会话级 override 的可见性：改过什么要一眼看得出来，否则用户会以为
            自己改的是全局默认（那两项在「⚙ AI 设置」里，含义不同）。 */}
        <button
          type="button"
          onClick={onRegenerate}
          disabled={running || !canRegenerate}
          title={canRegenerate ? '重跑最后一条提问，替换掉当前答案' : '还没有提问过'}
          className="text-ink-dim hover:text-up disabled:opacity-40"
        >
          ↻ 重新生成
        </button>
        <button type="button" onClick={onFork} className="text-ink-dim hover:text-up">
          换后端
        </button>
        <button type="button" onClick={onEdit} className="text-ink-dim hover:text-up">
          调整能力
        </button>
      </div>
    </div>
  );
}

/** `null` / 缺省 = 全开（没裁剪），与「空列表 = 一个都不给」要分得清。 */
function CountChip({ label, names }: { label: string; names?: string[] | null }) {
  return (
    <span className="tag">
      {label} {names == null ? '全部' : names.length === 0 ? '未启用' : `×${names.length}`}
    </span>
  );
}

/** 右栏对话窗口：历史加载、事件流订阅、流式渲染、中断、会话内改能力。 */
export default function ChatWindow({
  session,
  onSessionChange,
  onForked,
}: {
  session: AskSession;
  onSessionChange?: (updated: AskSession) => void;
  /** 换后端另开会话成功后回调（父级要把新会话加进列表并切过去） */
  onForked?: (created: AskSession) => void;
}) {
  const [msgs, setMsgs] = useState<AskMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [running, setRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [atBottom, setAtBottom] = useState(true);
  const [configOpen, setConfigOpen] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);
  const lastSentRef = useRef('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  //: 组件是否还挂着（中断兜底定时器用；卸载后不再 setState）
  const aliveRef = useRef(true);
  //: 运行代数：每轮回答 +1，收到 done/error 也 +1。中断兜底定时器据此判断
  //: 「我那次取消对应的还是不是当前这一轮」，避免误伤刚发起的新一轮。
  const runSeqRef = useRef(0);
  const sid = session.id;

  const loadHistory = useCallback(
    (alive: () => boolean) => {
      getMessages(sid)
        .then((list) => {
          if (alive()) setMsgs(list);
        })
        .catch(() => {
          if (alive()) setError('历史消息加载失败');
        });
    },
    [sid],
  );

  // 选中会话：加载历史 → 订阅事件流；切换会话取消旧订阅
  useEffect(() => {
    let live = true;
    const alive = () => live;
    aliveRef.current = true;
    setMsgs([]);
    setError(null);
    setRunning(false);
    setCancelling(false);
    setTrace(null);
    setAtBottom(true);
    loadHistory(alive);
    const cancel = connectAskEvents(
      sid,
      (ev: AgentEventMsg) => {
        if (ev.type === 'done' || ev.type === 'error') {
          setRunning(false);
          setCancelling(false);
          runSeqRef.current += 1; // 本轮结束：中断兜底定时器作废
          // done/error 的落库内容以拉库为准（中断标记「（已中断）」也在库里）
          loadHistory(alive);
        } else {
          setRunning(true);
          setNow(Date.now());
        }
        if (ev.type === 'error') setError(ev.message ?? '生成失败，请重试');
        if (TRACE_EVENTS.has(ev.type)) {
          const t = Date.now();
          setTrace((prev) => applyTraceEvent(prev ?? newRunTrace(t), ev, t));
        }
        setMsgs((prev) => reduceMessages(prev, ev));
      },
      undefined,
      // 重连成功：总线不回放，断线期间的事件只能靠重新拉库补上
      () => loadHistory(alive),
    );
    return () => {
      live = false;
      aliveRef.current = false;
      cancel();
    };
  }, [sid, loadHistory]);

  // 运行中：心跳驱动耗时显示（过程轨与输入区的「已运行 8.3s」）
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [running]);

  // 新事件自动滚底 —— 只在用户本来就在底部时。用户翻上去看历史时把视口抢回
  // 底部，是「不流畅」最典型的一种表现。
  useEffect(() => {
    if (!atBottom) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, trace, atBottom]);

  // pending 看门狗：服务端异常时可能不再补发 done，2 分钟无任何新事件则复位，
  // 避免输入框永久卡在「正在生成…」。复位不掩盖错误，只解除锁死。
  useEffect(() => {
    if (!running) return;
    const t = setTimeout(() => setRunning(false), 120_000);
    return () => clearTimeout(t);
  }, [running, msgs, trace]);

  const scrollToBottom = useCallback(() => {
    setAtBottom(true);
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const beginRun = useCallback(() => {
    const t = Date.now();
    runSeqRef.current += 1; // 新的运行代数：上一轮的中断兜底定时器随之作废
    setRunning(true);
    setCancelling(false);
    setError(null);
    setTrace(newRunTrace(t));
    setNow(t);
    setAtBottom(true);
  }, []);

  const pushLocalUser = useCallback(
    (content: string) => {
      const local: AskMessage = {
        id: `local-${Date.now()}`,
        session_id: sid,
        role: 'user',
        content,
        tool_calls: [],
        created_at: new Date().toISOString(),
      };
      setMsgs((prev) => [...prev, local]);
    },
    [sid],
  );

  const send = useCallback(async () => {
    const content = draft.trim();
    if (!content || running) return;
    setDraft('');
    lastSentRef.current = content;
    pushLocalUser(content);
    beginRun();
    try {
      await sendMessage(sid, content);
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [beginRun, draft, pushLocalUser, running, sid]);

  const retry = useCallback(async () => {
    const text = lastSentRef.current;
    if (!text || running) return;
    pushLocalUser(text);
    beginRun();
    try {
      await sendMessage(sid, text);
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [beginRun, pushLocalUser, running, sid]);

  /** 重新生成：后端会**先删掉旧答案**再跑，所以这里不等事件流 —— 立刻拉一次
   *  历史把旧答案从界面上抹掉，然后靠事件流把新一轮正文补上。 */
  const regenerate = useCallback(async () => {
    if (running) return;
    beginRun();
    try {
      await regenerateSession(sid);
      loadHistory(() => aliveRef.current);
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [beginRun, loadHistory, running, sid]);

  const stop = useCallback(async () => {    setCancelling(true);
    const seq = runSeqRef.current;
    try {
      await cancelSession(sid);
    } catch (e) {
      setCancelling(false);
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    // 收尾仍然以事件流里的 error 事件为准。但事件可能根本收不到（WS 断线时
    // 总线不回放），所以给一个兜底：3 秒后若这一轮还没结束就复位状态并拉库，
    // 免得界面永远卡在「正在中断…」。代数没变说明没有新一轮，复位是安全的。
    setTimeout(() => {
      if (!aliveRef.current || runSeqRef.current !== seq) return;
      setRunning(false);
      setCancelling(false);
      loadHistory(() => aliveRef.current);
    }, 3000);
  }, [loadHistory, sid]);

  const elapsed = trace ? (trace.finishedAt ?? now) - trace.startedAt : 0;
  const canRegenerate = msgs.some((m) => m.role === 'user');

  return (
    <div className="flex h-full flex-col">
      <SessionHeader
        session={session}
        running={running}
        canRegenerate={canRegenerate}
        onEdit={() => setConfigOpen(true)}
        onRegenerate={() => void regenerate()}
        onFork={() => setForkOpen(true)}
      />
      {trace ? <RunTraceView trace={trace} running={running} now={now} /> : null}

      {/* 消息流 */}
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4"
      >
        {msgs.length === 0 && !running && !error ? (
          <div className="border border-dashed border-line-strong bg-panel px-6 py-12 text-center text-sm text-ink-faint">
            还没有消息，输入问题开始对话
          </div>
        ) : null}
        {msgs.map((m) => (
          <Message key={m.id} message={m} />
        ))}
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

      {!atBottom ? (
        <div className="border-t border-line bg-panel px-3 py-1 text-right">
          <button type="button" className="btn btn-sm" onClick={scrollToBottom}>
            ↓ 回到最新
          </button>
        </div>
      ) : null}

      {/* 输入区：Enter 发送、Shift+Enter 换行 */}
      <div className="border-t border-line p-3">
        {running ? (
          <div className="mb-2 flex items-center justify-between text-xs text-ink-faint">
            <span>正在生成…{trace ? ` 已运行 ${fmtDuration(elapsed)}` : ''}</span>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void stop()}
              disabled={cancelling}
            >
              {cancelling ? '正在中断…' : '■ 停止'}
            </button>
          </div>
        ) : null}
        <div className="flex items-end gap-2">
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
          <button
            type="button"
            className="btn btn-primary shrink-0"
            onClick={() => void send()}
            disabled={running || !draft.trim()}
          >
            发送
          </button>
        </div>
      </div>

      {configOpen ? (
        <SessionConfigDialog
          session={session}
          onCancel={() => setConfigOpen(false)}
          onSaved={(updated) => {
            setConfigOpen(false);
            onSessionChange?.(updated);
          }}
        />
      ) : null}

      {forkOpen ? (
        <ForkDialog
          session={session}
          currentProvider={session.agent_config?.provider ?? ''}
          onCancel={() => setForkOpen(false)}
          onForked={(created) => {
            setForkOpen(false);
            onForked?.(created);
          }}
        />
      ) : null}
    </div>
  );
}
