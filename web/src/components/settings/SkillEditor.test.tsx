import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteSkill,
  getCapabilities,
  getSkill,
  putSkill,
  type AgentCapabilities,
} from '@/lib/agent-api';
import { renderPage } from '@/test/page-utils';
import SkillEditor from './SkillEditor';

vi.mock('@/lib/agent-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/agent-api')>();
  return {
    ...actual,
    getCapabilities: vi.fn(),
    getSkill: vi.fn(),
    putSkill: vi.fn(),
    deleteSkill: vi.fn(),
  };
});

const mockedCaps = vi.mocked(getCapabilities);
const mockedGet = vi.mocked(getSkill);
const mockedPut = vi.mocked(putSkill);
const mockedDel = vi.mocked(deleteSkill);

const caps: AgentCapabilities = {
  providers: [{ id: 'mock', label: 'Mock', available: true }],
  skills: [
    { name: 'alpha', description: '甲', tags: [], valid: true },
    { name: 'broken', description: '', tags: [], valid: false },
  ],
  mcp_tools: [],
  defaults: {},
};

beforeEach(() => {
  mockedCaps.mockResolvedValue(caps);
  mockedGet.mockImplementation(async (name) => ({ name, content: `# ${name} 正文` }));
  mockedPut.mockResolvedValue({ ok: true });
  mockedDel.mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('SkillEditor', () => {
  it('列出全部 skill（含 frontmatter 不合格的，标出来）', async () => {
    renderPage(<SkillEditor />);
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument());
    expect(screen.getByText('broken')).toBeInTheDocument();
    expect(screen.getByText('名称或 frontmatter 不合格')).toBeInTheDocument();
  });

  it('默认选中第一个并加载正文', async () => {
    renderPage(<SkillEditor />);
    await waitFor(() => expect(mockedGet).toHaveBeenCalledWith('alpha'));
    expect(await screen.findByDisplayValue('# alpha 正文')).toBeInTheDocument();
  });

  it('切换选中项会加载对应正文', async () => {
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.click(screen.getByText('broken'));
    await waitFor(() => expect(mockedGet).toHaveBeenCalledWith('broken'));
    expect(await screen.findByDisplayValue('# broken 正文')).toBeInTheDocument();
  });

  it('保存：把编辑后的原文 PUT 出去并刷新清单', async () => {
    renderPage(<SkillEditor />);
    const ta = await screen.findByDisplayValue('# alpha 正文');
    fireEvent.change(ta, { target: { value: '# alpha 改过' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedPut).toHaveBeenCalledWith('alpha', '# alpha 改过'));
    expect(await screen.findByText('✓ 已保存 alpha')).toBeInTheDocument();
    expect(mockedCaps.mock.calls.length).toBeGreaterThan(1); // mutate 重新拉清单
  });

  it('保存失败：展示后端错误，不假装成功', async () => {
    mockedPut.mockRejectedValue(new Error('frontmatter 缺少 description'));
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(await screen.findByText('✗ frontmatter 缺少 description')).toBeInTheDocument();
  });

  it('删除：confirm 后才调 DELETE', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    expect(mockedDel).not.toHaveBeenCalled();

    vi.stubGlobal('confirm', vi.fn(() => true));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(mockedDel).toHaveBeenCalledWith('alpha'));
  });

  it('新建：用模板起头，且不去 GET 这个还不存在的名字', async () => {
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    mockedGet.mockClear();
    fireEvent.change(screen.getByPlaceholderText('新 skill 名'), {
      target: { value: 'my-new' },
    });
    fireEvent.click(screen.getByRole('button', { name: '新建' }));
    const ta = screen.getByLabelText('my-new 的 SKILL.md') as HTMLTextAreaElement;
    expect(ta.value).toContain('name: my-new');
    expect(ta.value).not.toContain('my-skill');
    // 关键：不能去 GET 一个服务端还没有的 skill —— 那个 404 会把引导文案冲掉
    expect(mockedGet).not.toHaveBeenCalled();
    expect(screen.getByText(/新建「my-new」/)).toBeInTheDocument();
    expect(screen.queryByText(/✗/)).not.toBeInTheDocument();
  });

  it('新建时名字撞上已有 skill：切到编辑态，不拿模板覆盖', async () => {
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.change(screen.getByPlaceholderText('新 skill 名'), {
      target: { value: 'alpha' },
    });
    fireEvent.click(screen.getByRole('button', { name: '新建' }));
    expect(await screen.findByText('「alpha」已存在，已切到编辑')).toBeInTheDocument();
    // 内容仍是服务端那份，不是模板
    expect(await screen.findByDisplayValue('# alpha 正文')).toBeInTheDocument();
  });

  it('从别的 skill 切过去新建同名时，提示不被加载 effect 冲掉', async () => {
    // 回归：加载 effect 里清 msg 的话，setSelected 触发的这次加载会把提示当场擦掉
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.click(screen.getByText('broken'));
    await screen.findByDisplayValue('# broken 正文');
    fireEvent.change(screen.getByPlaceholderText('新 skill 名'), {
      target: { value: 'alpha' },
    });
    fireEvent.click(screen.getByRole('button', { name: '新建' }));
    expect(await screen.findByText('「alpha」已存在，已切到编辑')).toBeInTheDocument();
  });

  it('加载正文失败：显示错误而不是空编辑器', async () => {
    mockedGet.mockRejectedValue(new Error('skill 不存在: alpha'));
    renderPage(<SkillEditor />);
    expect(await screen.findByText('✗ skill 不存在: alpha')).toBeInTheDocument();
  });
});
