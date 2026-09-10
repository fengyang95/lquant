import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentEventMsg, AskMessage, AskSession } from '@/lib/ask-api';
import { connectAskEvents, getMessages, sendMessage } from '@/lib/ask-api';
import ChatWindow from './ChatWindow';

vi.mock('@/lib/ask-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ask-api')>();
  return {
    ...actual,
    getMessages: vi.fn(),
    sendMessage: vi.fn(),
    connectAskEvents: vi.fn(() => vi.fn()),
  };
});

const mockedGetMessages = vi.mocked(getMessages);
const mockedSendMessage = vi.mocked(sendMessage);
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
});
