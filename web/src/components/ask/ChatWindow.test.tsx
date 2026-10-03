import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentEventMsg, AskMessage, AskSession } from '@/lib/ask-api';
import { cancelSession, connectAskEvents, getMessages, sendMessage } from '@/lib/ask-api';
import ChatWindow from './ChatWindow';

vi.mock('@/lib/ask-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ask-api')>();
  return {
    ...actual,
    getMessages: vi.fn(),
    sendMessage: vi.fn(),
    cancelSession: vi.fn(),
    connectAskEvents: vi.fn(() => vi.fn()),
  };
});

const mockedGetMessages = vi.mocked(getMessages);
const mockedSendMessage = vi.mocked(sendMessage);
const mockedCancel = vi.mocked(cancelSession);
const mockedConnect = vi.mocked(connectAskEvents);

const session: AskSession = {
  id: 's1',
  title: '测试会话',
  context: { symbol: '600519' },
  created_at: '2026-01-01T00:00:00Z',
};

function msg(partial: Partial<AskMessage>): AskMessage {
  return {
    id: partial.id ?? 'm1',
    session_id: 's1',
    role: 'user',
    content: '',
    tool_calls: [],
    created_at: '2026-01-01T00:00:00Z',
    ...partial,
  };
}

let capturedOnEvent: ((ev: AgentEventMsg) => void) | null = null;

beforeEach(() => {
  capturedOnEvent = null;
  vi.clearAllMocks();
  mockedConnect.mockImplementation((_sid, onEvent) => {
    capturedOnEvent = onEvent;
    return vi.fn();
  });
});

describe('ChatWindow', () => {
  it('渲染历史消息', async () => {
    mockedGetMessages.mockResolvedValue([
      msg({ id: 'u1', role: 'user', content: '你好' }),
      msg({ id: 'a1', role: 'assistant', content: '你好，请问有什么可以帮您？' }),
    ]);
    render(<ChatWindow session={session} />);
    expect(await screen.findByText('你好')).toBeInTheDocument();
    expect(screen.getByText('你好，请问有什么可以帮您？')).toBeInTheDocument();
  });

  it('发送：Enter 调用 sendMessage 并乐观上屏用户消息', async () => {
    mockedGetMessages.mockResolvedValue([]);
    mockedSendMessage.mockResolvedValue({ user_message: msg({ content: '帮我查一下茅台' }) });
    render(<ChatWindow session={session} />);

    const input = await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');
    fireEvent.change(input, { target: { value: '帮我查一下茅台' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => {
      expect(mockedSendMessage).toHaveBeenCalledWith('s1', '帮我查一下茅台');
    });
    // 乐观上屏
    expect(screen.getByText('帮我查一下茅台')).toBeInTheDocument();
  });

  it('收到 assistant_delta 事件时消息流更新，done 时对账', async () => {
    mockedGetMessages.mockResolvedValue([]);
    render(<ChatWindow session={session} />);
    await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');

    await waitFor(() => {
      expect(capturedOnEvent).not.toBeNull();
    });
    const onEvent = capturedOnEvent as (ev: AgentEventMsg) => void;

    onEvent({ type: 'assistant_delta', message_id: 'a9', text: '贵州茅台' });
    onEvent({ type: 'assistant_delta', message_id: 'a9', text: '近一年上涨' });
    expect(await screen.findByText(/贵州茅台近一年上涨/)).toBeInTheDocument();

    // done：触发 getMessages 对账替换
    mockedGetMessages.mockResolvedValue([
      msg({ id: 'a9', role: 'assistant', content: '贵州茅台近一年上涨 12%（落库版）' }),
    ]);
    onEvent({ type: 'done' });
    await screen.findByText('贵州茅台近一年上涨 12%（落库版）');
  });

  it('过程轨：thinking 与工具调用上屏，结果配回对应工具', async () => {
    mockedGetMessages.mockResolvedValue([]);
    render(<ChatWindow session={session} />);
    await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());
    const onEvent = capturedOnEvent as (ev: AgentEventMsg) => void;

    onEvent({ type: 'thinking', text: '先确认口径' });
    onEvent({ type: 'tool_call', name: 'get_quotes', args: { symbol: '600519' } });
    // 时长随真实时钟走（「推理 · 0ms」可能变成 1ms），只断言步骤本身
    expect(await screen.findByText(/^推理/)).toBeInTheDocument();
    expect(screen.getByText('先确认口径')).toBeInTheDocument();
    expect(screen.getByText('get_quotes')).toBeInTheDocument();
    expect(screen.getByText('symbol=600519')).toBeInTheDocument();

    onEvent({ type: 'tool_result', name: 'get_quotes', summary: '1500 元', text: '贵州茅台 1500 元' });
    // 摘要进 summary、全文进折叠的 pre
    expect(await screen.findByText('贵州茅台 1500 元')).toBeInTheDocument();
    expect(screen.getAllByText(/1500 元/).length).toBeGreaterThan(0);
  });

  it('运行中给「停止」按钮，点击调 cancelSession', async () => {
    mockedGetMessages.mockResolvedValue([]);
    mockedCancel.mockResolvedValue(undefined);
    render(<ChatWindow session={session} />);
    await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());
    const onEvent = capturedOnEvent as (ev: AgentEventMsg) => void;

    expect(screen.queryByRole('button', { name: /停止/ })).toBeNull();
    onEvent({ type: 'thinking', text: '想一下' });
    const stop = await screen.findByRole('button', { name: '■ 停止' });
    fireEvent.click(stop);
    await waitFor(() => expect(mockedCancel).toHaveBeenCalledWith('s1'));

    // error 事件（中断）后回到空闲态，停止按钮消失
    onEvent({ type: 'error', message: '已中断' });
    await waitFor(() => expect(screen.queryByRole('button', { name: /停止/ })).toBeNull());
    expect(await screen.findByText('已中断')).toBeInTheDocument();
  });

  it('中断后收不到收尾事件：3 秒兜底复位，不会永远卡在「正在中断…」', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockedGetMessages.mockResolvedValue([]);
      mockedCancel.mockResolvedValue(undefined);
      render(<ChatWindow session={session} />);
      await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');
      await waitFor(() => expect(capturedOnEvent).not.toBeNull());
      const onEvent = capturedOnEvent as (ev: AgentEventMsg) => void;

      onEvent({ type: 'thinking', text: '想一下' });
      fireEvent.click(await screen.findByRole('button', { name: '■ 停止' }));
      await waitFor(() => expect(mockedCancel).toHaveBeenCalledWith('s1'));
      // 后端事件因 WS 断线永远到不了：兜底定时器到点后必须自己复位
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3100);
      });
      expect(screen.queryByRole('button', { name: /停止|正在中断/ })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('发送按钮：草稿为空时禁用，有内容时可点', async () => {
    mockedGetMessages.mockResolvedValue([]);
    render(<ChatWindow session={session} />);
    const input = await screen.findByPlaceholderText('输入问题，Enter 发送，Shift+Enter 换行');
    const btn = screen.getByRole('button', { name: '发送' });
    expect(btn).toBeDisabled();
    fireEvent.change(input, { target: { value: '大盘怎么样' } });
    expect(btn).not.toBeDisabled();
  });
});
