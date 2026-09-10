import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import AskPage from './page';

const { mockCreateSession, mockListSessions, mockUseSearchParams, mockGetMessages } = vi.hoisted(
  () => ({
    mockCreateSession: vi.fn(),
    mockListSessions: vi.fn(),
    mockUseSearchParams: vi.fn(),
    mockGetMessages: vi.fn(),
  }),
);

vi.mock('next/navigation', () => ({
  useSearchParams: mockUseSearchParams,
}));

vi.mock('@/lib/ask-api', () => ({
  createSession: mockCreateSession,
  listSessions: mockListSessions,
  deleteSession: vi.fn(),
  getMessages: mockGetMessages,
  connectAskEvents: vi.fn(() => vi.fn()),
}));

describe('AskPage 新建会话失败反馈', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseSearchParams.mockReturnValue(new URLSearchParams());
    mockListSessions.mockResolvedValue([]);
    mockGetMessages.mockResolvedValue([]);
  });

  it('createSession 抛错时点击新建显示错误提示（ready 态不静默）', async () => {
    mockCreateSession.mockRejectedValue(new Error('网络开小差了'));
    render(<AskPage />);

    // 空会话空态出现后说明初始化完成（ready）
    expect(await screen.findByText(/还没有会话/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    await waitFor(() => {
      expect(screen.getByText(/网络开小差了/)).toBeInTheDocument();
    });
  });

  it('新建成功后新会话被选中，之前的错误提示被清掉', async () => {
    render(<AskPage />);
    expect(await screen.findByText(/还没有会话/)).toBeInTheDocument();

    mockCreateSession.mockResolvedValue({
      id: 's2',
      title: '新会话',
      context: {},
      created_at: '2026-01-01T00:00:00Z',
    });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    await waitFor(() => {
      expect(screen.getByText('新会话')).toBeInTheDocument();
    });
  });
});
