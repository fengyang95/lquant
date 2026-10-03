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
    // provider 单选不存在 —— 但运行参数那组「跟随默认 / 开 / 关」是三态覆盖，
    // 不是 provider 选择，所以按名字断言而不是「一个 radio 都没有」。
    expect(screen.queryByRole('radio', { name: /Claude Code|Codex|Mock/ })).toBeNull();
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
      // 未指定 → 显式回写 null（= 跟随全局默认），不是 0 / false
      timeout_seconds: null,
      skip_permissions: null,
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
      timeout_seconds: null,
      skip_permissions: null,
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

  it('会话级超时：未设过 → 输入框为空；填 600 → 请求体里是数字 600', async () => {
    mockedUpdate.mockResolvedValue(session);
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);

    const input = await screen.findByPlaceholderText('跟随默认');
    // 未设过必须是空串（不能预填成 0 / 默认值，否则保存一次就把全局默认冻成会话值）
    expect((input as HTMLInputElement).value).toBe('');

    fireEvent.change(input, { target: { value: '600' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    expect(mockedUpdate.mock.calls[0][1].timeout_seconds).toBe(600);
  });

  it('会话级超时留空 = 跟随全局 → 回写 null，绝不是 0', async () => {
    mockedUpdate.mockResolvedValue(session);
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);

    await screen.findByPlaceholderText('跟随默认');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    // 0 会被当成「本会话超时为 0 秒」这个合法覆盖值，和「不覆盖」完全两回事
    expect(mockedUpdate.mock.calls[0][1].timeout_seconds).toBeNull();
    expect(mockedUpdate.mock.calls[0][1].timeout_seconds).not.toBe(0);
  });

  it('会话级超时填非数字 → 本地拦下并说明原因，不发请求', async () => {
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);

    fireEvent.change(await screen.findByPlaceholderText('跟随默认'), {
      target: { value: 'abc' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await screen.findByText(/超时必须是整数秒/)).toBeInTheDocument();
    expect(mockedUpdate).not.toHaveBeenCalled();
  });

  it('权限三态：选「本会话关闭」→ skip_permissions: false（明确关，不是不覆盖）', async () => {
    mockedUpdate.mockResolvedValue(session);
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);

    fireEvent.click(await screen.findByRole('radio', { name: '本会话关闭' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    expect(mockedUpdate.mock.calls[0][1].skip_permissions).toBe(false);
  });

  it('权限三态：已有 skip_permissions=false 预填「本会话关闭」；改回「跟随默认」→ null 而不是 false', async () => {
    const permissionsOff = {
      ...session,
      agent_config: { provider: 'claude_code', skip_permissions: false },
    };
    mockedUpdate.mockResolvedValue(session);
    render(
      <SessionConfigDialog session={permissionsOff} onCancel={vi.fn()} onSaved={vi.fn()} />,
    );

    expect(await screen.findByRole('radio', { name: '本会话关闭' })).toBeChecked();
    fireEvent.click(screen.getByRole('radio', { name: '跟随默认' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    // null = 回到全局默认档，与「这一会话明确关掉」不是一回事
    expect(mockedUpdate.mock.calls[0][1].skip_permissions).toBeNull();
  });

  it('skip_permissions 未设时预填「跟随默认」，不能被误当成「本会话关闭」', async () => {
    render(<SessionConfigDialog session={session} onCancel={vi.fn()} onSaved={vi.fn()} />);
    expect(await screen.findByRole('radio', { name: '跟随默认' })).toBeChecked();
    expect(screen.getByRole('radio', { name: '本会话关闭' })).not.toBeChecked();
  });
});
