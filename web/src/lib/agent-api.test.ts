import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deleteSkill,
  encodeSelection,
  getCapabilities,
  getSkill,
  putSkill,
  renderSkillTemplate,
  resolveDefaults,
  SKILL_NAME_RE,
  type AgentCapabilities,
} from './agent-api';
import { okEnvelope, stubPageFetch } from '@/test/page-utils';

const caps: AgentCapabilities = {
  providers: [
    { id: 'claude_code', label: 'Claude Code', available: true },
    { id: 'codex', label: 'Codex', available: false },
  ],
  skills: [{ name: 'alpha', description: 'A', tags: [], valid: true }],
  mcp_tools: [{ name: 'get_quotes', description: '行情' }],
  defaults: { provider: 'codex', skills: null, mcp_tools: null },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('resolveDefaults', () => {
  it('defaults 里的 null = 全开 → 全部勾上', () => {
    expect(resolveDefaults(caps)).toEqual({
      provider: 'codex',
      skills: ['alpha'],
      mcpTools: ['get_quotes'],
    });
  });

  it('defaults 给了名单就照名单（空列表 = 一个都不勾）', () => {
    const out = resolveDefaults({
      ...caps,
      defaults: { provider: null, skills: [], mcp_tools: ['get_quotes'] },
    });
    expect(out.provider).toBe('claude_code'); // 没给 provider → 取第一个可选项
    expect(out.skills).toEqual([]);
    expect(out.mcpTools).toEqual(['get_quotes']);
  });
});

describe('API 调用', () => {
  it('getCapabilities 解封套', async () => {
    stubPageFetch({ '/agent/capabilities': { code: 0, data: caps, message: 'ok' } });
    expect(await getCapabilities()).toEqual(caps);
  });

  it('skill 名做 URL 编码（防注入到路径里）', async () => {
    const fetchMock = stubPageFetch({
      '/agent/skills/': { code: 0, data: { name: 'a b', content: 'x' }, message: 'ok' },
    });
    await getSkill('a b');
    await putSkill('a b', '内容');
    await deleteSkill('a b');
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.every((u) => u.includes('a%20b'))).toBe(true);
    expect(urls.some((u) => u.endsWith('/agent/skills/a%20b'))).toBe(true);
  });

  it('putSkill 把 content 放进请求体', async () => {
    const fetchMock = stubPageFetch({
      '/agent/skills/': { code: 0, data: { ok: true }, message: 'ok' },
    });
    await putSkill('alpha', '---\nname: alpha\n---\n');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({ content: '---\nname: alpha\n---\n' });
  });

  it('封套 code!=0 → 抛错并带上后端 message', async () => {
    stubPageFetch({
      '/agent/skills/': { code: 1, data: null, message: '非法 skill 名: ../x' },
    });
    await expect(putSkill('x', 'y')).rejects.toThrow('非法 skill 名: ../x');
  });

  it('okEnvelope 生成的响应也能被解出（与后端封套同构）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okEnvelope(caps)));
    expect(await getCapabilities()).toEqual(caps);
  });
});

describe('encodeSelection —— 全选回写成 null（不裁剪）', () => {
  it('全选 → null；部分选 → 显式名单', () => {
    expect(encodeSelection(['a', 'b'], ['a', 'b'])).toBeNull();
    expect(encodeSelection(['a'], ['a', 'b'])).toEqual(['a']);
    expect(encodeSelection([], ['a', 'b'])).toEqual([]);
  });

  it('可选项为空时也是 null（写入 [] 会把「不裁剪」降级成「一个都不启用」）', () => {
    expect(encodeSelection([], [])).toBeNull();
  });
});

describe('renderSkillTemplate —— 新建 skill 的骨架', () => {
  it('frontmatter 的 name / description 与正文标题都换成新名字', () => {
    const out = renderSkillTemplate('limit-up-scan', '涨停扫描：找连板');
    expect(out).toContain('name: limit-up-scan');
    expect(out).toContain('description: "涨停扫描：找连板"');
    expect(out).toContain('# limit-up-scan');
    expect(out).not.toContain('my-skill');
  });

  it('描述里的 $& / $` 不被当成替换模式（否则静默写出坏 frontmatter）', () => {
    const out = renderSkillTemplate('alpha', '成本 $& 占比');
    expect(out).toContain('description: "成本 $& 占比"');
    // $& 若被解释，会把匹配到的整行原文插进来 —— 那行是模板里的默认描述
    expect(out).not.toContain('一句话说明这个 skill');
  });

  it('反斜杠与双引号被转义，frontmatter 不会被写坏', () => {
    const out = renderSkillTemplate('alpha', '路径 C:\\data 与 "引号"');
    expect(out).toContain('description: "路径 C:\\\\data 与 \\"引号\\""');
    // 描述行必须单行且引号闭合
    const line = out.split('\n').find((l) => l.startsWith('description: '));
    expect(line?.endsWith('"')).toBe(true);
  });

  it('换行被压成空格（多行描述会破坏 frontmatter）', () => {
    const out = renderSkillTemplate('alpha', '第一行\n第二行');
    expect(out).toContain('description: "第一行 第二行"');
  });

  it('名字形状与后端同一口径', () => {
    expect(SKILL_NAME_RE.test('a-stock-data')).toBe(true);
    expect(SKILL_NAME_RE.test('Bad Name')).toBe(false);
    expect(SKILL_NAME_RE.test('-lead')).toBe(false);
  });
});
