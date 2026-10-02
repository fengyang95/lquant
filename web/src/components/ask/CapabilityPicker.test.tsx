import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import { getCapabilities, putSkill } from '@/lib/agent-api';
import CapabilityPicker from './CapabilityPicker';

vi.mock('@/lib/agent-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/agent-api')>();
  return { ...actual, putSkill: vi.fn(), getCapabilities: vi.fn() };
});

const mockedPut = vi.mocked(putSkill);
const mockedCaps = vi.mocked(getCapabilities);

const caps: AgentCapabilities = {
  providers: [{ id: 'claude_code', label: 'Claude Code', available: true }],
  skills: [{ name: 'a-stock-data', description: 'A 股数据', tags: [], valid: true }],
  mcp_tools: [{ name: 'get_quotes', description: '行情' }],
  defaults: { provider: 'claude_code', skills: ['a-stock-data'], mcp_tools: null },
};

function setup(overrides: Partial<React.ComponentProps<typeof CapabilityPicker>> = {}) {
  const props = {
    caps,
    onCapsChange: vi.fn(),
    provider: 'claude_code',
    onProviderChange: vi.fn(),
    skills: ['a-stock-data'],
    onSkillsChange: vi.fn(),
    tools: ['get_quotes'],
    onToolsChange: vi.fn(),
    ...overrides,
  };
  render(<CapabilityPicker {...props} />);
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CapabilityPicker', () => {
  it('providerLocked 时后端只读，不渲染单选', () => {
    setup({ providerLocked: true });
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.getByText(/建会话时锁定/)).toBeInTheDocument();
  });

  it('就地新建 skill：落盘 → 刷新清单 → 自动勾上', async () => {
    const next: AgentCapabilities = {
      ...caps,
      skills: [...caps.skills, { name: 'limit-up-scan', description: '涨停扫描', tags: [], valid: true }],
    };
    mockedPut.mockResolvedValue({ ok: true });
    mockedCaps.mockResolvedValue(next);
    const onSkillsChange = vi.fn();
    const props = setup({ onSkillsChange });

    fireEvent.click(screen.getByRole('button', { name: '＋ 新建 skill' }));
    fireEvent.change(screen.getByPlaceholderText(/新 skill 名/), {
      target: { value: 'limit-up-scan' },
    });
    fireEvent.change(screen.getByPlaceholderText(/一句话描述/), {
      target: { value: '涨停扫描' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并勾选' }));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(1));
    const [name, content] = mockedPut.mock.calls[0];
    expect(name).toBe('limit-up-scan');
    // frontmatter 必须带 name / description —— 缺了它 skill 在能力清单里等于不存在
    expect(content).toContain('name: limit-up-scan');
    expect(content).toContain('description: "涨停扫描"');
    await waitFor(() => expect(props.onCapsChange).toHaveBeenCalledWith(next));
    // 勾选走 setState 的 updater 形态（异步新建期间用户可能又动了别的勾选）
    const updater = onSkillsChange.mock.calls[0][0] as (prev: string[]) => string[];
    expect(typeof updater).toBe('function');
    expect(updater(['a-stock-data'])).toEqual(['a-stock-data', 'limit-up-scan']);
  });

  it('名字不合形状 / 描述为空：本地先挡，不发请求', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: '＋ 新建 skill' }));
    fireEvent.change(screen.getByPlaceholderText(/新 skill 名/), { target: { value: 'Bad Name' } });
    fireEvent.change(screen.getByPlaceholderText(/一句话描述/), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并勾选' }));
    expect(await screen.findByText(/只能用小写字母/)).toBeInTheDocument();
    expect(mockedPut).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText(/新 skill 名/), { target: { value: 'ok-name' } });
    fireEvent.change(screen.getByPlaceholderText(/一句话描述/), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并勾选' }));
    expect(await screen.findByText(/写一句描述/)).toBeInTheDocument();
    expect(mockedPut).not.toHaveBeenCalled();
  });

  it('名字撞上已有 skill：提示而不覆盖', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: '＋ 新建 skill' }));
    fireEvent.change(screen.getByPlaceholderText(/新 skill 名/), {
      target: { value: 'a-stock-data' },
    });
    fireEvent.change(screen.getByPlaceholderText(/一句话描述/), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并勾选' }));
    expect(await screen.findByText(/已存在同名 skill/)).toBeInTheDocument();
    expect(mockedPut).not.toHaveBeenCalled();
  });

  it('全选只勾可用的 skill', () => {
    const broken: AgentCapabilities = {
      ...caps,
      skills: [...caps.skills, { name: 'broken', description: '', tags: [], valid: false }],
    };
    const props = setup({ caps: broken, skills: [] });
    fireEvent.click(screen.getAllByRole('button', { name: '全选' })[0]);
    expect(props.onSkillsChange).toHaveBeenCalledWith(['a-stock-data']);
  });
});
