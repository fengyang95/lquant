import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import { getCapabilities, putSkill } from '@/lib/agent-api';
import CapabilityPicker, { invertSelection } from './CapabilityPicker';

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

describe('invertSelection —— 只在可选集合内取反', () => {
  it('全选 → []；空 → 按 all 的顺序全选', () => {
    expect(invertSelection(['a', 'b'], ['a', 'b'])).toEqual([]);
    expect(invertSelection([], ['a', 'b', 'c'])).toEqual(['a', 'b', 'c']);
  });

  it('部分取反：all 内选中的变未选、未选的变选中', () => {
    expect(invertSelection(['a'], ['a', 'b', 'c'])).toEqual(['b', 'c']);
    expect(invertSelection(['b', 'c'], ['a', 'b', 'c'])).toEqual(['a']);
  });

  it('all 之外已选的名字保留：失效 skill 不该被反选顺手清掉', () => {
    // 'stale' 只可能是目录被删/改名后失效的 skill —— 用户没点「清空」，
    // 反选就没有裁掉它的授权（清掉等于替用户做了一次他没要求的裁剪）
    expect(invertSelection(['a', 'stale'], ['a', 'b'])).toEqual(['b', 'stale']);
    expect(invertSelection(['stale'], ['a', 'b'])).toEqual(['a', 'b', 'stale']);
    expect(invertSelection(['a', 'b', 'stale'], ['a', 'b'])).toEqual(['stale']);
  });
});

/** 受控壳：把 onSkillsChange 接上真实 state，才能断言点击后渲染出的勾选状态 */
function StatefulPicker({ initial, caps: c }: { initial: string[]; caps: AgentCapabilities }) {
  const [skills, setSkills] = useState(initial);
  const [tools, setTools] = useState<string[]>([]);
  return (
    <CapabilityPicker
      caps={c}
      onCapsChange={vi.fn()}
      provider="claude_code"
      skills={skills}
      onSkillsChange={setSkills}
      tools={tools}
      onToolsChange={setTools}
    />
  );
}

describe('CapabilityPicker 反选按钮', () => {
  const multi: AgentCapabilities = {
    providers: [{ id: 'claude_code', label: 'Claude Code', available: true }],
    skills: [
      { name: 'alpha', description: '甲', tags: [], valid: true },
      { name: 'beta', description: '乙', tags: [], valid: true },
      { name: 'gamma', description: '丙', tags: [], valid: true },
    ],
    mcp_tools: [],
    defaults: {},
  };

  it('点了「反选」后勾选状态整体翻转（不是只翻当前视图里看得见的）', () => {
    render(<StatefulPicker initial={['alpha']} caps={multi} />);
    expect(screen.getByRole('checkbox', { name: /alpha/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /beta/ })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: /gamma/ })).not.toBeChecked();

    fireEvent.click(screen.getAllByRole('button', { name: '反选' })[0]);

    expect(screen.getByRole('checkbox', { name: /alpha/ })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: /beta/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /gamma/ })).toBeChecked();
  });

  it('反选的 updater 对 all 之外的已选名字保持不动（语义边界：不替用户裁剪）', () => {
    const onSkillsChange = vi.fn();
    setup({ caps: multi, skills: ['alpha', 'stale'], onSkillsChange });
    fireEvent.click(screen.getAllByRole('button', { name: '反选' })[0]);
    const updater = onSkillsChange.mock.calls[0][0] as (prev: string[]) => string[];
    expect(typeof updater).toBe('function');
    expect(updater(['alpha', 'stale'])).toEqual(['beta', 'gamma', 'stale']);
  });
});
