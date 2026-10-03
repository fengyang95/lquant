import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteSkill,
  getCapabilities,
  getSkill,
  putSkill,
  readSkillFrontmatter,
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

  it('搜索：只留下匹配项；搜不到时说明搜的是什么，而不是给一个空列表', async () => {
    renderPage(<SkillEditor />);
    await screen.findByText('alpha');
    const search = screen.getByLabelText('搜索 skill');

    fireEvent.change(search, { target: { value: 'alp' } });
    expect(screen.getByText('alpha')).toBeInTheDocument();
    expect(screen.queryByText('broken')).toBeNull();

    fireEvent.change(search, { target: { value: 'zzz' } });
    expect(screen.getByText('没有匹配「zzz」的 skill')).toBeInTheDocument();
    expect(screen.queryByText('alpha')).toBeNull();
  });

  it('frontmatter 不合格的 skill：提示不可用，一键补全后内容合法且提示消失', async () => {
    const badBody = '# broken 正文\n\n步骤一：先查什么。';
    mockedGet.mockImplementation(async (name) => ({
      name,
      content:
        name === 'broken' ? badBody : '---\nname: alpha\ndescription: 甲\n---\n\n# alpha',
    }));
    renderPage(<SkillEditor />);
    const alphaTa = (await screen.findByLabelText('alpha 的 SKILL.md')) as HTMLTextAreaElement;
    await waitFor(() => expect(alphaTa.value).toContain('name: alpha'));

    fireEvent.click(screen.getByText('broken'));
    const ta = (await screen.findByLabelText('broken 的 SKILL.md')) as HTMLTextAreaElement;
    expect(ta.value).toBe(badBody);
    // 不合格的 skill 在能力清单里选不了，用户至少要被告知为什么
    expect(screen.getByText(/这个 skill 目前/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /补全 frontmatter/ }));

    const repaired = (screen.getByLabelText('broken 的 SKILL.md') as HTMLTextAreaElement).value;
    const fm = readSkillFrontmatter(repaired);
    expect(fm?.name).toBe('broken'); // name 取目录名，与 list_skills 的 id 对齐
    expect(fm?.description).toBeTruthy();
    // 正文必须原样保留：用户的正文才是他写这个 skill 的目的
    expect(repaired).toContain(badBody);
    // 修完提示要消失，否则用户会以为没修好
    expect(screen.queryByText(/这个 skill 目前/)).toBeNull();
  });

  it('已合规的 skill 不显示「不可用」提示（没有缺项就不该弹警告）', async () => {
    mockedGet.mockResolvedValue({
      name: 'alpha',
      content: '---\nname: alpha\ndescription: 甲\n---\n正文',
    });
    renderPage(<SkillEditor />);
    const ta = (await screen.findByLabelText('alpha 的 SKILL.md')) as HTMLTextAreaElement;
    await waitFor(() => expect(ta.value).toBe('---\nname: alpha\ndescription: 甲\n---\n正文'));
    expect(screen.queryByText(/这个 skill 目前/)).toBeNull();
    expect(screen.queryByRole('button', { name: /补全 frontmatter/ })).toBeNull();
  });

  it('新建态不显示「不可用」提示：内容还没写完是正常中间状态', async () => {
    renderPage(<SkillEditor />);
    await screen.findByDisplayValue('# alpha 正文');
    fireEvent.change(screen.getByPlaceholderText('新 skill 名'), {
      target: { value: 'my-new' },
    });
    fireEvent.click(screen.getByRole('button', { name: '新建' }));

    const ta = screen.getByLabelText('my-new 的 SKILL.md');
    // 故意把 frontmatter 删掉：新建态下也不该弹警告（isNew 一律豁免）
    fireEvent.change(ta, { target: { value: '# 还没写 frontmatter' } });
    expect(screen.queryByText(/这个 skill 目前/)).toBeNull();
    expect(screen.queryByRole('button', { name: /补全 frontmatter/ })).toBeNull();
  });
});
