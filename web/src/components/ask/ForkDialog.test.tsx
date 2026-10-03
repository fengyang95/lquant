import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import type { AskSession } from '@/lib/ask-api';
import { forkSession } from '@/lib/ask-api';
import { stubPageFetch } from '@/test/page-utils';
import ForkDialog from './ForkDialog';

// 只替掉 forkSession：getCapabilities 走真实现 + stubPageFetch，
// 才能顺带断言「打开弹层就拉了 /agent/capabilities」这个请求本身。
vi.mock('@/lib/ask-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ask-api')>();
  return { ...actual, forkSession: vi.fn() };
});

const mockedFork = vi.mocked(forkSession);

const caps: AgentCapabilities = {
  providers: [
    { id: 'claude_code', label: 'Claude Code', available: true },
    { id: 'codex', label: 'Codex', available: true },
    { id: 'mock', label: 'Mock', available: false },
  ],
  skills: [],
  mcp_tools: [],
  defaults: {},
};

const session: AskSession = {
  id: 's1',
  title: '测试会话',
  context: { symbol: '600519' },
  created_at: '2026-01-01T00:00:00Z',
  agent_config: { provider: 'claude_code' },
};

function stubCaps(c: AgentCapabilities = caps) {
  return stubPageFetch({ '/agent/capabilities': { code: 0, data: c, message: 'ok' } });
}

function setup(currentProvider = 'claude_code') {
  const props = { session, currentProvider, onCancel: vi.fn(), onForked: vi.fn() };
  render(<ForkDialog {...props} />);
  return props;
}

beforeEach(() => {
  stubCaps();
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('ForkDialog', () => {
  it('打开就拉 /agent/capabilities；当前 provider 不列出来（同一后端另开会后端会 400）', async () => {
    const fetchMock = stubCaps();
    setup('claude_code');
    expect(await screen.findByRole('radio', { name: /Codex/ })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Mock/ })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((c) => String(c[0]).includes('/agent/capabilities')),
    ).toBe(true);
    // 当前 provider 没有选项：不是「禁用它」，而是根本不给这个必错的选项
    expect(screen.queryByRole('radio', { name: /Claude Code/ })).toBeNull();
  });

  it('未选目标时「另开会话」禁用；选中后提交 → forkSession(id, provider) 并回调新会话', async () => {
    const created: AskSession = {
      ...session,
      id: 's2',
      title: '测试会话（Codex）',
      agent_config: { provider: 'codex' },
    };
    mockedFork.mockResolvedValue(created);
    const props = setup('claude_code');

    const submit = screen.getByRole('button', { name: '另开会话' });
    expect(submit).toBeDisabled();

    fireEvent.click(await screen.findByRole('radio', { name: /Codex/ }));
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    await waitFor(() => expect(mockedFork).toHaveBeenCalledWith('s1', 'codex'));
    await waitFor(() => expect(props.onForked).toHaveBeenCalledWith(created));
  });

  it('不可用的后端标出「选了会启动失败」，但仍可选（用户自己判断）', async () => {
    setup('claude_code');
    const mock = await screen.findByRole('radio', { name: /Mock/ });
    expect(mock).not.toBeDisabled();
    expect(screen.getByText(/本机 PATH 上没有这个 CLI/)).toBeInTheDocument();
  });

  it('forkSession 抛错 → 原文留在弹层里，不静默也不自动关闭', async () => {
    mockedFork.mockRejectedValue(new Error('目标 provider 与会话相同'));
    const props = setup('claude_code');

    fireEvent.click(await screen.findByRole('radio', { name: /Codex/ }));
    fireEvent.click(screen.getByRole('button', { name: '另开会话' }));

    expect(await screen.findByText('目标 provider 与会话相同')).toBeInTheDocument();
    // 弹层还在：用户要能换个后端重试，而不是从头再填一遍
    expect(screen.getByRole('dialog', { name: '换后端另开会话' })).toBeInTheDocument();
    expect(props.onForked).not.toHaveBeenCalled();
  });

  it('只剩当前一个后端时给出「没有别的后端可选」，而不是一个空列表', async () => {
    stubCaps({ ...caps, providers: [caps.providers[0]] });
    setup('claude_code');

    expect(await screen.findByText(/没有别的后端可选/)).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.getByRole('button', { name: '另开会话' })).toBeDisabled();
  });
});
