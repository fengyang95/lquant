import type { AgentConfig } from './agent-api';
import { delData, getData, patchData, postData } from './api';

/** 会话列表项 */
export interface AskSession {
  id: string;
  title: string;
  context: Record<string, unknown>;
  created_at: string;
  /** 会话级能力配置。`provider` 建会话时锁定（换后端会续到别人的 CLI 会话），
   *  `skills` / `mcp_tools` 可在会话内改（见 updateSessionConfig）。
   *  `{}` = 未指定，走全局默认。 */
  agent_config?: AgentConfig;
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
  type:
    | 'assistant_delta'
    | 'thinking'
    | 'tool_call'
    | 'tool_result'
    | 'system'
    | 'done'
    | 'error';
  text?: string;
  name?: string;
  summary?: string;
  args?: Record<string, unknown>;
  data?: Record<string, unknown>;
  message_id?: string;
  message?: string;
}

const BASE = '/ask';

export function listSessions(): Promise<AskSession[]> {
  return getData<AskSession[]>(`${BASE}/sessions`);
}

/** 建会话；`agentConfig` 传了就在建会话时锁定能力集（provider 建后锁定）。 */
export function createSession(
  context: Record<string, unknown> = {},
  agentConfig?: AgentConfig,
): Promise<AskSession> {
  // /api/ask 是封套路由：走 *Data 系列解包；失败时后端的 message 才会透出来
  return postData<AskSession>(`${BASE}/sessions`, { context, ...(agentConfig ?? {}) });
}

export function deleteSession(id: string): Promise<void> {
  return delData<void>(`${BASE}/sessions/${encodeURIComponent(id)}`);
}

/** 改会话的 skill / MCP 工具。`provider` 不在可改之列（传了后端会 400）。
 *
 *  改动**下一轮生效**：工作区每轮都按会话配置重建，所以不用重开会话，
 *  也不用等当前这轮跑完。走 `patchData`（封套接口）：400 的后端文案
 *  （「provider 建会话时锁定，不可修改」）要能原样透到弹层里。 */
export function updateSessionConfig(id: string, cfg: AgentConfig): Promise<AskSession> {
  return patchData<AskSession>(`${BASE}/sessions/${encodeURIComponent(id)}/config`, cfg);
}

/** 中断当前回答：服务端会 cancel 后台任务并 terminate CLI 子进程。
 *  中断的最终结果仍以事件流里的 error 事件为准，这里只负责发指令。
 *  走 `postData`：404（会话已删）的后端文案要能透出来，别退化成「404 /xxx」。 */
export async function cancelSession(id: string): Promise<void> {
  await postData<void>(`${BASE}/sessions/${encodeURIComponent(id)}/cancel`, {});
}

export function getMessages(id: string): Promise<AskMessage[]> {
  return getData<AskMessage[]>(`${BASE}/sessions/${encodeURIComponent(id)}/messages`);
}

/** POST 202，封套 data 里是 {user_message, agent_task}；这里只取 user_message。
 *
 *  走 `postData` 而不是裸 `post`：同会话单飞时后端回 409「该会话已有正在执行的
 *  回答」，裸 post 只读 body.detail，会把这条人话吞成「409 /api/ask/...」。 */
export function sendMessage(
  id: string,
  content: string,
): Promise<{ user_message: AskMessage }> {
  return postData<{ user_message: AskMessage }>(
    `${BASE}/sessions/${encodeURIComponent(id)}/messages`, { content });
}

/** 重新生成：重跑最后一条提问，**替换**掉它后面的回答。
 *
 *  后端会先删掉旧回答再跑，所以调用方收到 202 后直接拉一次消息即可看到
 *  「旧答案已消失」；`replaced_messages` 是删掉的条数（0 也正常：上一轮失败
 *  没留下回答）。
 *
 *  走 `postData`：400（还没提问）/ 409（正在跑）的后端文案要能透出来。 */
export function regenerateSession(
  id: string,
): Promise<{ user_message: AskMessage; replaced_messages: number }> {
  return postData<{ user_message: AskMessage; replaced_messages: number }>(
    `${BASE}/sessions/${encodeURIComponent(id)}/regenerate`, {});
}

/** 换后端另开会话：复制本会话的上下文与能力集，在**另一个 provider** 上建新会话。
 *
 *  为什么不是「改 provider」：CLI 侧会话 id（claude 的 session_id / codex 的
 *  thread_id）共用一列，中途换 provider 续接的是另一个 CLI 的会话，上下文会串。
 *  老会话原样保留，新会话通过 `context.briefing` 拿到老会话的对话简报。 */
export function forkSession(id: string, provider: string): Promise<AskSession> {
  return postData<AskSession>(
    `${BASE}/sessions/${encodeURIComponent(id)}/fork`, { provider });
}

/** 一个正在跑的 agent 回答（`GET /ask/runs`） */
export interface RunInfo {
  session_id: string;
  provider: string;
  elapsed_seconds: number;
  /** CLI 子进程 pid；mock 这类无子进程的 provider 为 null */
  pid: number | null;
  workspace: string;
}

export interface RunsSnapshot {
  runs: RunInfo[];
  max_concurrent_runs: number;
}

/** 正在跑的 agent（跨 provider）。用于「谁在跑、跑了多久、pid 多少」。 */
export function listRuns(): Promise<RunsSnapshot> {
  return getData<RunsSnapshot>(`${BASE}/runs`);
}

/** 终止某条会话正在跑的回答（运行中列表里直接点掉，不用先切会话）。 */
export function killRun(id: string): Promise<{ ok: boolean; killed: boolean }> {
  return postData<{ ok: boolean; killed: boolean }>(
    `${BASE}/runs/${encodeURIComponent(id)}/kill`, {});
}

/** 把 Agent 事件流归约进消息列表（纯函数，不可变更新）。
 *  done/error 原样返回——落库消息的最终替换由调用方拉取完成。 */
export function reduceMessages(msgs: AskMessage[], ev: AgentEventMsg): AskMessage[] {
  if (ev.type === 'done' || ev.type === 'error') return msgs;

  // 过程数据（thinking / system）不进消息流：它们是**本轮运行**的过程，
  // 由 ask-stream 的 RunTrace 单独渲染；混进正文会污染落库对账。
  if (ev.type === 'thinking' || ev.type === 'system') return msgs;

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
 *
 *  - 服务端在 done/error 后**不关连接**（同一会话可继续提问），所以这里收到
 *    done 只回调 onDone，不停止重连 —— 旧写法把 done 当成终态，一旦这之后
 *    连接掉了（代理超时、后端重启），下一次提问就再也收不到任何事件，界面
 *    只能卡在「正在生成…」直到看门狗超时。
 *  - 事件总线**不回放**：断线期间的事件只能靠调用方重新拉落库消息对账，
 *    所以重连成功时回调 onReconnect（首次连接不回调）。
 *  - 返回的取消函数用于组件卸载：停止重连意图并关闭连接。 */
export function connectAskEvents(
  sid: string,
  onEvent: (ev: AgentEventMsg) => void,
  onDone?: () => void,
  onReconnect?: () => void,
): () => void {
  const host = window.location.host;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${host}/ws/ask/${encodeURIComponent(sid)}`;

  let ws: WebSocket | null = null;
  let cancelled = false;
  let opened = false;
  let retry = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const connect = (): void => {
    if (cancelled) return;
    ws = new WebSocket(url);
    ws.onopen = () => {
      if (cancelled) return;
      if (opened) onReconnect?.();
      opened = true;
      retry = 0; // 连上了就把退避清零，别让一次抖动永久抬高重连延迟
    };
    ws.onmessage = (e: MessageEvent) => {
      let ev: AgentEventMsg;
      try {
        ev = JSON.parse(String(e.data)) as AgentEventMsg;
      } catch {
        return; // 非 JSON 消息静默忽略
      }
      if (ev.type === 'done') onDone?.();
      onEvent(ev);
    };
    ws.onclose = () => {
      if (cancelled) return;
      const delay = Math.min(1000 * 2 ** retry, 10000);
      retry += 1;
      retryTimer = setTimeout(connect, delay);
    };
  };

  connect();

  return () => {
    cancelled = true;
    if (retryTimer !== null) clearTimeout(retryTimer);
    ws?.close();
  };
}
