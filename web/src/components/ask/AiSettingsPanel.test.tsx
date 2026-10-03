import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import { getCapabilities, getSkill } from '@/lib/agent-api';
import type { SettingItem } from '@/lib/settings-api';
import { listSettings, putSetting, resetSetting } from '@/lib/settings-api';
import { renderPage } from '@/test/page-utils';
import AiSettingsPanel, { capabilityValue } from './AiSettingsPanel';

vi.mock('@/lib/agent-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/agent-api')>();
  return { ...actual, getCapabilities: vi.fn(), getSkill: vi.fn() };
});

vi.mock('@/lib/settings-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/settings-api')>();
  return {
    ...actual,
    listSettings: vi.fn(),
    putSetting: vi.fn(),
    resetSetting: vi.fn(),
  };
});

const mockedCaps = vi.mocked(getCapabilities);
const mockedGetSkill = vi.mocked(getSkill);
const mockedList = vi.mocked(listSettings);
const mockedPut = vi.mocked(putSetting);
const mockedReset = vi.mocked(resetSetting);

const caps: AgentCapabilities = {
  providers: [
    { id: 'claude_code', label: 'Claude Code', available: true },
    { id: 'codex', label: 'Codex', available: false },
  ],
  skills: [
    { name: 'alpha', description: '甲', tags: [], valid: true },
    { name: 'beta', description: '乙', tags: [], valid: true },
    { name: 'broken', description: '', tags: [], valid: false },
  ],
  mcp_tools: [
    { name: 'get_quotes', description: '行情' },
    { name: 'get_daily', description: '日线' },
  ],
  // defaults 全 null = 不裁剪 → 可选项全部预填勾上
  defaults: { provider: 'claude_code', skills: null, mcp_tools: null },
};

const settings: SettingItem[] = [
  {
    key: 'agent.provider', value: 'claude_code', type: 'enum', source: 'config',
    label: '问 AI 后端', choices: ['claude_code', 'codex'],
  },
  {
    key: 'agent.default_skills', value: 'all', type: 'str', source: 'default',
    label: '新建会话默认 skill', choices: null,
  },
  {
    key: 'agent.default_mcp_tools', value: 'all', type: 'str', source: 'default',
    label: '新建会话默认 MCP 工具', choices: null,
  },
  {
    key: 'agent.timeout_seconds', value: 300, type: 'int', source: 'default',
    label: '超时（秒）', choices: null,
  },
  {
    key: 'agent.skip_permissions', value: 'true', type: 'bool', source: 'runtime',
    label: '全自主权限', choices: null,
  },
  {
    key: 'agent.partial_messages', value: 'false', type: 'bool', source: 'runtime',
    label: 'token 级流式', choices: null,
  },
  {
    key: 'agent.max_concurrent_runs', value: 4, type: 'int', source: 'default',
    label: '并发上限', choices: null,
  },
];

function renderPanel(): ReturnType<typeof vi.fn> {
  const onClose = vi.fn();
  renderPage(<AiSettingsPanel onClose={onClose} />);
  return onClose;
}

/** 等首屏（能力清单 + 配置）都到齐：默认能力 tab 出现 skill 勾选框 */
const firstPaint = () => screen.findByRole('checkbox', { name: /alpha/ });

beforeEach(() => {
  vi.clearAllMocks();
  mockedCaps.mockResolvedValue(caps);
  mockedList.mockResolvedValue(settings);
  mockedGetSkill.mockImplementation(async (name) => ({ name, content: `# ${name} 正文` }));
  mockedPut.mockResolvedValue({ key: '', value: '' });
  mockedReset.mockResolvedValue({ key: '', reset: true });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('capabilityValue —— all / none / 名单 三态不能混', () => {
  it('全选 → all（不裁剪：以后新增的 skill / 工具自动带上）', () => {
    expect(capabilityValue(['alpha', 'beta'], ['alpha', 'beta'])).toBe('all');
  });

  it('一个都不选 → none（与 all 语义完全相反，绝不能写成空串）', () => {
    expect(capabilityValue([], ['alpha', 'beta'])).toBe('none');
  });

  it('子集 → 逗号名单，保持勾选顺序', () => {
    expect(capabilityValue(['alpha'], ['alpha', 'beta'])).toBe('alpha');
    expect(capabilityValue(['beta', 'alpha'], ['alpha', 'beta', 'gamma'])).toBe('beta,alpha');
  });

  it('可选项为空集时仍是 all（空集的全选 = 不裁剪）', () => {
    expect(capabilityValue([], [])).toBe('all');
  });
});

describe('AiSettingsPanel 打开', () => {
  it('并行拉 /agent/capabilities 与 /settings，渲染三个 tab 与默认能力预填', async () => {
    renderPanel();

    await firstPaint();
    expect(mockedCaps).toHaveBeenCalledTimes(1);
    expect(mockedList).toHaveBeenCalledTimes(1);

    expect(screen.getByRole('dialog', { name: 'AI 设置' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '默认能力' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Skill 编辑' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行参数' })).toBeInTheDocument();

    // defaults.provider 预填；null 能力集预填全部可选项，坏 skill 不勾也不可选
    expect(screen.getByRole('radio', { name: /Claude Code/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /beta/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /get_daily/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /broken/ })).toBeDisabled();
  });

  it('切到 Skill 编辑 tab 渲染 SkillEditor', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getByRole('button', { name: 'Skill 编辑' }));
    expect(await screen.findByLabelText('alpha 的 SKILL.md')).toBeInTheDocument();
  });

  it('关闭按钮调用 onClose', async () => {
    const onClose = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('AiSettingsPanel 默认能力保存', () => {
  it('全选保存：连续 PUT provider / default_skills / default_mcp_tools，全选写成 all', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getByRole('button', { name: '保存默认能力' }));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(3));
    expect(mockedPut.mock.calls).toEqual([
      ['agent.provider', 'claude_code'],
      ['agent.default_skills', 'all'],
      ['agent.default_mcp_tools', 'all'],
    ]);
  });

  it('取消一个 skill：default_skills 存显式名单', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getByRole('checkbox', { name: /beta/ })); // 取消 beta
    fireEvent.click(screen.getByRole('button', { name: '保存默认能力' }));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(3));
    expect(mockedPut).toHaveBeenCalledWith('agent.default_skills', 'alpha');
    expect(mockedPut).toHaveBeenCalledWith('agent.default_mcp_tools', 'all');
  });

  it('清空全部 skill：default_skills 存 none（不是 all）', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getAllByRole('button', { name: '清空' })[0]); // Skill 区
    fireEvent.click(screen.getByRole('button', { name: '保存默认能力' }));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(3));
    expect(mockedPut).toHaveBeenCalledWith('agent.default_skills', 'none');
    expect(mockedPut).toHaveBeenCalledWith('agent.default_mcp_tools', 'all');
  });

  it('清空全部 MCP 工具：default_mcp_tools 存 none', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getAllByRole('button', { name: '清空' })[1]); // MCP 区
    fireEvent.click(screen.getByRole('button', { name: '保存默认能力' }));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(3));
    expect(mockedPut).toHaveBeenCalledWith('agent.default_mcp_tools', 'none');
  });

  it('恢复配置默认：依次 reset skill 与 MCP 两项（不是 PUT）', async () => {
    renderPanel();
    await firstPaint();

    fireEvent.click(screen.getByRole('button', { name: '恢复配置默认' }));

    await waitFor(() => expect(mockedReset).toHaveBeenCalledTimes(2));
    expect(mockedReset.mock.calls.map((c) => c[0])).toEqual([
      'agent.default_skills',
      'agent.default_mcp_tools',
    ]);
    expect(mockedPut).not.toHaveBeenCalled();
  });
});

describe('AiSettingsPanel 运行参数', () => {
  const openRuntime = async () => {
    renderPanel();
    await firstPaint();
    fireEvent.click(screen.getByRole('button', { name: '运行参数' }));
  };

  it('开关状态来自 settings 字符串布尔值', async () => {
    await openRuntime();
    expect(await screen.findByRole('checkbox', { name: /全自主权限/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /token 级流式/ })).not.toBeChecked();
  });

  it('取消「全自主权限」立即 PUT agent.skip_permissions=false（字符串布尔）', async () => {
    await openRuntime();

    fireEvent.click(await screen.findByRole('checkbox', { name: /全自主权限/ }));

    await waitFor(() =>
      expect(mockedPut).toHaveBeenCalledWith('agent.skip_permissions', 'false'),
    );
  });

  it('勾上「token 级流式」立即 PUT agent.partial_messages=true', async () => {
    await openRuntime();
    await screen.findByRole('checkbox', { name: /全自主权限/ });

    fireEvent.click(screen.getByRole('checkbox', { name: /token 级流式/ }));

    await waitFor(() =>
      expect(mockedPut).toHaveBeenCalledWith('agent.partial_messages', 'true'),
    );
  });

  it('改超时后点保存：PUT agent.timeout_seconds，值是输入框里的数字文本', async () => {
    await openRuntime();

    const input = await screen.findByDisplayValue('300');
    fireEvent.change(input, { target: { value: '600' } });
    fireEvent.click(screen.getByRole('button', { name: '保存超时' }));

    await waitFor(() =>
      expect(mockedPut).toHaveBeenCalledWith('agent.timeout_seconds', '600'),
    );
  });

  it('改并发上限后点保存：PUT agent.max_concurrent_runs（超出区间由后端拦）', async () => {
    await openRuntime();

    const input = await screen.findByDisplayValue('4');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并发上限' }));

    await waitFor(() =>
      expect(mockedPut).toHaveBeenCalledWith('agent.max_concurrent_runs', '8'),
    );
  });

  it('已被 runtime 覆盖的项显示「重置」入口，点击 DELETE 对应 key 并提示', async () => {
    await openRuntime();

    // timeout 是 default，不显示重置；skip / partial 是 runtime，各一个
    const resets = await screen.findAllByRole('button', { name: '已覆盖 · 重置为配置默认' });
    expect(resets).toHaveLength(2);

    fireEvent.click(resets[0]);
    await waitFor(() =>
      expect(mockedReset).toHaveBeenCalledWith('agent.skip_permissions'),
    );
    expect(await screen.findByText('✓ 全自主权限 已重置为配置默认')).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole('button', { name: '已覆盖 · 重置为配置默认' })[1]);
    await waitFor(() =>
      expect(mockedReset).toHaveBeenCalledWith('agent.partial_messages'),
    );
  });

  it('保存失败：把后端原因显示在面板里，不静默', async () => {
    mockedPut.mockRejectedValue(new Error('超时应在 10~3600 秒之间，收到 5'));
    await openRuntime();

    const input = await screen.findByDisplayValue('300');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: '保存超时' }));

    expect(
      await screen.findByText('✗ 超时应在 10~3600 秒之间，收到 5'),
    ).toBeInTheDocument();
  });
});
