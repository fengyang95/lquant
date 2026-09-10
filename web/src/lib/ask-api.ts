import { ApiError, delData, getData, post } from './api';

/** 会话列表项 */
export interface AskSession {
  id: string;
  title: string;
  context: Record<string, unknown>;
  created_at: string;
}

/** 工具调用记录 */
export interface AskToolCall {
  name?: string;
  args?: Record<string, unknown>;
}

/** 消息（toolDone 为前端流式归约出的临时标记，非后端字段） */
export interface AskMessage {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  tool_calls: AskToolCall[];
  created_at: string;
  toolDone?: boolean;
}

/** Agent 事件流消息（WebSocket 下发） */
export interface AgentEventMsg {
  type: 'assistant_delta' | 'tool_call' | 'tool_result' | 'done' | 'error';
  text?: string;
  name?: string;
  summary?: string;
  args?: Record<string, unknown>;
  message_id?: string;
  message?: string;
}

/** 后端封套 {code,data,message,trace_id} */
interface Envelope<T> {
  code: number;
  data: T;
  message?: string;
  trace_id?: string;
}

const BASE = '/ask';

export function listSessions(): Promise<AskSession[]> {
  return getData<AskSession[]>(`${BASE}/sessions`);
}

export function createSession(context: Record<string, unknown> = {}): Promise<AskSession> {
  return post(`${BASE}/sessions`, { context }).then((body) => {
    // /api/ask 为封套接口：post 不解包，这里自行取 data
    const env = body as unknown as Envelope<AskSession>;
    if (env && typeof env === 'object' && 'code' in env) {
      if (env.code !== 0) throw new ApiError(200, env.message || '创建会话失败');
      return env.data;
    }
    return body as unknown as AskSession;
  });
}

export function deleteSession(id: string): Promise<void> {
  return delData<void>(`${BASE}/sessions/${encodeURIComponent(id)}`);
}

export function getMessages(id: string): Promise<AskMessage[]> {
  return getData<AskMessage[]>(`${BASE}/sessions/${encodeURIComponent(id)}/messages`);
}

/** POST 返回 202，封套 data 里是 {user_message, agent_task}；这里只取 user_message */
export async function sendMessage(
  id: string,
  content: string,
): Promise<{ user_message: AskMessage }> {
  const body = await post(`${BASE}/sessions/${encodeURIComponent(id)}/messages`, { content });
  const env = body as unknown as Envelope<{ user_message: AskMessage }>;
  if (env && typeof env === 'object' && 'code' in env) {
    if (env.code !== 0) throw new ApiError(200, env.message || '发送失败');
    return env.data;
  }
  return body as unknown as { user_message: AskMessage };
}

/** 把 Agent 事件流归约进消息列表（纯函数，不可变更新）。
 *  done/error 原样返回——落库消息的最终替换由调用方拉取完成。 */
export function reduceMessages(msgs: AskMessage[], ev: AgentEventMsg): AskMessage[] {
  if (ev.type === 'done' || ev.type === 'error') return msgs;

  if (ev.type === 'tool_result') {
    // 给最后一条 assistant 消息打标记；没有 assistant 消息则忽略
    const idx = findLastAssistant(msgs);
    if (idx < 0) return msgs;
    return msgs.map((m, i) => (i === idx ? { ...m, toolDone: true } : m));
  }

  if (ev.type === 'tool_call') {
    const idx = findLastAssistant(msgs);
    const call: AskToolCall = { name: ev.name, args: ev.args };
    if (idx < 0) {
      return [
        ...msgs,
        {
          id: '',
          session_id: '',
          role: 'assistant',
          content: '',
          tool_calls: [call],
          created_at: '',
        },
      ];
    }
    return msgs.map((m, i) =>
      i === idx ? { ...m, tool_calls: [...m.tool_calls, call] } : m,
    );
  }

  // assistant_delta：按 message_id 找目标消息，找不到则新建
  const mid = ev.message_id ?? '';
  const idx = msgs.findIndex((m) => m.id === mid);
  const text = ev.text ?? '';
  if (idx < 0) {
    return [
      ...msgs,
      {
        id: mid,
        session_id: '',
        role: 'assistant',
        content: text,
        tool_calls: [],
        created_at: '',
      },
    ];
  }
  return msgs.map((m, i) => (i === idx ? { ...m, content: m.content + text } : m));
}

function findLastAssistant(msgs: AskMessage[]): number {
  for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return i;
  return -1;
}


/** 连接会话事件流：ws(s)://host/ws/ask/{sid}，断线指数退避重连（1s 起上限 10s）。
 *  收到 done 事件时触发 onDone 并停止重连意图；cancel 只负责停止重连并关闭连接，不触发 onDone。
 *  返回取消函数。 */
export function connectAskEvents(
  sid: string,
  onEvent: (ev: AgentEventMsg) => void,
  onDone?: () => void,
): () => void {
  const host = window.location.host;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${host}/ws/ask/${encodeURIComponent(sid)}`;

  let ws: WebSocket | null = null;
  let cancelled = false;
  let finished = false;
  let retry = 0;

  const connect = (): void => {
    ws = new WebSocket(url);
    ws.onmessage = (e: MessageEvent) => {
      let ev: AgentEventMsg;
      try {
        ev = JSON.parse(String(e.data)) as AgentEventMsg;
      } catch {
        return; // 非 JSON 消息静默忽略
      }
      if (ev.type === 'done' && !finished) {
        finished = true;
        onDone?.();
      }
      onEvent(ev);
    };
    ws.onclose = () => {
      if (cancelled || finished) return; // done 后不再重连
      const delay = Math.min(1000 * 2 ** retry, 10000);
      retry += 1;
      setTimeout(connect, delay);
    };
  };

  connect();

  return () => {
    cancelled = true;
    ws?.close();
  };
}
