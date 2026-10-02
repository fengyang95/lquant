import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import { getCapabilities } from '@/lib/agent-api';
import type { AskSession } from '@/lib/ask-api';
import { updateSessionConfig } from '@/lib/ask-api';
import SessionConfigDialog from './SessionConfigDialog';

vi.mock('@/lib/agent-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/agent-api')>();
  return { ...actual, getCapabilities: vi.fn() };
});

vi.mock('@/lib/ask-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ask-api')>();
  return { ...actual, updateSessionConfig: vi.fn() };
});

const mockedCaps = vi.mocked(getCapabilities);
const mockedUpdate = vi.mocked(updateSessionConfig);

const caps: AgentCapabilities = {
  providers: [{ id: 'claude_code', label: 'Claude Code', available: true }],
  skills: [
    { name: 'alpha', description: '甲', tags: [], valid: true },
    { name: 'beta', description: '乙', tags: [], valid: true },
    { name: 'broken', description: '', tags: [], valid: false },
  ],
  mcp_tools: [
    { name: 'get_quotes', description: '行情' },
    { name: 'get_daily', description: '日线' },
  ],
  defaults: {},
};

const session: AskSession = {
  id: 's1',
  title: '测试会话',
  context: {},
  created_at: '2026-01-01T00:00:00Z',
  agent_config: { provider: 'claude_code' },
};

beforeEach(() => {
  vi.clearAllMocks();
  mockedCaps.mockResolvedValue(caps);
});

describe('SessionConfigDialog', () => {
  it('provider 只读（换了会续到别的 CLI 会话）', async () => {
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);
    expect(await screen.findByText(/建会话时锁定/)).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
  });

  it('未指定能力（全开）时预填全选，保存回写成 null 而不是快照', async () => {
    mockedUpdate.mockResolvedValue({
      ...session,
      agent_config: { provider: 'claude_code', skills: null, mcp_tools: null },
    });
    const onSaved = vi.fn();
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={onSaved} />);

    // 不合格的 skill 不参与预填（勾上只会让后端 400）
    expect(await screen.findByRole('checkbox', { name: /alpha/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /beta/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /broken/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith('s1', {
      skills: null, // 全选 = 不裁剪：以后新增的 skill 也自动带上
      mcp_tools: null,
    }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it('取消勾选后保存的是显式名单', async () => {
    mockedUpdate.mockResolvedValue(session);
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(await screen.findByRole('checkbox', { name: /beta/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /get_daily/ }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith('s1', {
      skills: ['alpha'],
      mcp_tools: ['get_quotes'],
    }));
  });

  it('保存失败：错误留在弹层里，不静默关掉', async () => {
    mockedUpdate.mockRejectedValue(new Error('provider 建会话时锁定，不可修改'));
    const onSaved = vi.fn();
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={onSaved} />);
    fireEvent.click(await screen.findByRole('button', { name: '保存' }));
    expect(await screen.findByText(/provider 建会话时锁定/)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('能力清单拉不到：给出错误而不是空白', async () => {
    mockedCaps.mockRejectedValue(new Error('500 /agent/capabilities'));
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);
    expect(await screen.findByText(/能力清单加载失败/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });
});
