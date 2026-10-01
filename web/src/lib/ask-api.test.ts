import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  connectAskEvents,
  reduceMessages,
  type AgentEventMsg,
  type AskMessage,
} from './ask-api';

const mk = (over: Partial<AskMessage> = {}): AskMessage => ({
  id: 'm1',
  session_id: 's1',
  role: 'assistant',
  content: '',
  tool_calls: [],
  created_at: '',
  ...over,
});

describe('reduceMessages', () => {
  it('1. delta 新建 assistant 消息并追加（不改原数组）', () => {
    const orig: AskMessage[] = [mk({ id: 'u1', role: 'user', content: '问题' })];
    const out = reduceMessages(orig, {
      type: 'assistant_delta',
      text: '你好',
      message_id: 'a1',
    });
    expect(orig).toHaveLength(1); // 原数组未变
    expect(orig[0].id).toBe('u1');
    expect(out).toHaveLength(2);
    expect(out[1]).toMatchObject({
      id: 'a1',
      role: 'assistant',
      content: '你好',
      tool_calls: [],
    });
    // append 到尾部（契约 6）
  });

  it('2. 同 message_id 的 delta 顺序追加 content', () => {
    let msgs: AskMessage[] = reduceMessages([], {
      type: 'assistant_delta',
      text: '你好',
      message_id: 'a1',
    });
    msgs = reduceMessages(msgs, {
      type: 'assistant_delta',
      text: '，世界',
      message_id: 'a1',
    });
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('你好，世界');
  });

  it('2b. 不同 message_id 的 delta 新建独立消息', () => {
    let msgs = reduceMessages([], { type: 'assistant_delta', text: 'A', message_id: 'a1' });
    msgs = reduceMessages(msgs, { type: 'assistant_delta', text: 'B', message_id: 'a2' });
    expect(msgs.map((m) => m.id)).toEqual(['a1', 'a2']);
  });

  it('3. tool_call 追加进最后一条 assistant 消息的 tool_calls', () => {
    let msgs = reduceMessages([], {
      type: 'assistant_delta',
      text: '查',
      message_id: 'a1',
    });
    msgs = reduceMessages(msgs, {
      type: 'tool_call',
      name: 'get_quote',
      args: { symbols: ['600519'] },
    });
    expect(msgs).toHaveLength(1);
    expect(msgs[0].tool_calls).toContainEqual({ name: 'get_quote', args: { symbols: ['600519'] } });
  });

  it('3b. 无 assistant 消息时 tool_call 新建占位消息', () => {
    const msgs = reduceMessages([], {
      type: 'tool_call',
      name: 'get_quote',
      args: { symbols: ['600519'] },
    });
    expect(msgs).toHaveLength(1);
    expect(msgs[0].role).toBe('assistant');
    expect(msgs[0].tool_calls).toContainEqual({ name: 'get_quote', args: { symbols: ['600519'] } });
  });

  it('3c. thinking / system 是过程数据，不进正文', () => {
    const base = reduceMessages([], {
      type: 'assistant_delta',
      text: 'A',
      message_id: 'a1',
    });
    // 原样返回（同一引用）：否则会掉进 assistant_delta 分支被当成正文累积
    expect(reduceMessages(base, { type: 'thinking', text: '先查行情' })).toBe(base);
    expect(reduceMessages(base, { type: 'system', data: { model: 'm' } })).toBe(base);
    expect(base[0].content).toBe('A');
  });

  it('4. tool_result 置 toolDone=true 且不进 content', () => {
    let msgs: AskMessage[] = reduceMessages([], {
      type: 'assistant_delta',
      text: '查',
      message_id: 'a1',
    });
    msgs = reduceMessages(msgs, { type: 'tool_result', name: 'get_quote', summary: 'ok' });
    expect(msgs[0].toolDone).toBe(true);
    expect(msgs[0].content).toBe('查'); // summary 未混入 content
    expect(msgs[0].tool_calls).toHaveLength(0);
  });

  it('5. done/error 原样返回输入数组', () => {
    const orig: AskMessage[] = [mk({ id: 'u1', role: 'user' })];
    expect(reduceMessages(orig, { type: 'done', message: 'ok' })).toBe(orig);
    expect(reduceMessages(orig, { type: 'error', message: 'x' })).toBe(orig);
  });
});

describe('connectAskEvents', () => {
  class FakeWebSocket {
    static instances: FakeWebSocket[] = [];
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    url: string;
    onmessage: ((e: { data: string }) => void) | null = null;
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    closed = false;
    constructor(url: string) {
      this.url = url;
      FakeWebSocket.instances.push(this);
    }
    close() {
      this.closed = true;
    }
  }

  const last = () => FakeWebSocket.instances.at(-1)!;
  let events: AgentEventMsg[];
  let onEvent: (e: AgentEventMsg) => void;
  let onDone: () => void;

  afterEach(() => {
    vi.useRealTimers();
    // @ts-expect-error 测试 stub
    delete globalThis.WebSocket;
  });

  const setup = () => {
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    events = [];
    onEvent = (e) => events.push(e);
    onDone = vi.fn();
    vi.useFakeTimers();
    setLocDefault();
  };

  const setLocDefault = (): void => {
    Object.defineProperty(window, 'location', {
      value: { protocol: 'http:', host: 'localhost:3000' },
      writable: true,
      configurable: true,
    });
  };

  const setLoc = (protocol: string, host: string): void => {
    Object.defineProperty(window, 'location', {
      value: { protocol, host },
      writable: true,
      configurable: true,
    });
  };

  it('连 ws://host/ws/ask/{sid} 并解析事件', () => {
    setup();
    setLoc('http:', 'localhost:3000');
    const cancel = connectAskEvents('s1', onEvent, onDone);
    expect(last().url).toBe('ws://localhost:3000/ws/ask/s1');

    last().onmessage?.({ data: 'not json' }); // 解析失败静默忽略
    expect(events).toHaveLength(0);

    last().onmessage?.({
      data: JSON.stringify({ type: 'assistant_delta', text: 'hi', message_id: 'a1' }),
    });
    expect(events).toEqual([{ type: 'assistant_delta', text: 'hi', message_id: 'a1' }]);
    cancel();
  });

  it('wss:// 在 https 下', () => {
    setup();
    setLoc('https:', 'example.com');
    const cancel = connectAskEvents('s2', onEvent);
    expect(last().url).toBe('wss://example.com/ws/ask/s2');
    cancel();
  });

  it('断线后指数退避重连，取消函数停止重连并关闭', () => {
    setup();
    const cancel = connectAskEvents('s1', onEvent);
    expect(FakeWebSocket.instances).toHaveLength(1);

    last().onclose?.(); // 断线 → 1s 后重连
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    last().onclose?.(); // 第二次 → 2s
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    cancel(); // 取消：停止重连并关闭当前连接
    last().onclose?.();
    vi.advanceTimersByTime(10_000);
    expect(FakeWebSocket.instances).toHaveLength(3);
    expect(last().closed).toBe(true);
  });

  it('onDone 仅在收到 done 事件时触发，并停止重连意图', () => {
    setup();
    const cancel = connectAskEvents('s1', onEvent, onDone);

    last().onmessage?.({ data: JSON.stringify({ type: 'assistant_delta', text: 'hi' }) });
    expect(onDone).not.toHaveBeenCalled();

    last().onmessage?.({ data: JSON.stringify({ type: 'done' }) });
    expect(onDone).toHaveBeenCalledTimes(1);

    // done 后连接关闭不再重连
    last().onclose?.();
    vi.advanceTimersByTime(10_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    cancel();
    expect(onDone).toHaveBeenCalledTimes(1); // cancel 不重复触发
  });

  it('cancel/清理不再调用 onDone', () => {
    setup();
    const cancel = connectAskEvents('s1', onEvent, onDone);
    last().onmessage?.({ data: JSON.stringify({ type: 'assistant_delta', text: 'hi' }) });
    cancel();
    expect(onDone).not.toHaveBeenCalled();
  });
});
