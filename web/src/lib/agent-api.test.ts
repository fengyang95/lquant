import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deleteSkill,
  encodeSelection,
  getCapabilities,
  getSkill,
  putSkill,
  readSkillFrontmatter,
  renderSkillTemplate,
  repairSkillContent,
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

describe('readSkillFrontmatter —— 与后端 parse_frontmatter 同口径', () => {
  it('首行不是 --- → null：这是「没有 frontmatter」，不是「字段为空」', () => {
    expect(readSkillFrontmatter('# 标题\nname: alpha\n')).toBeNull();
    expect(readSkillFrontmatter('name: alpha\ndescription: 甲')).toBeNull();
    expect(readSkillFrontmatter('')).toBeNull();
  });

  it('只有开头 --- 没有结束 --- → null（半截 frontmatter 不按合格算）', () => {
    expect(readSkillFrontmatter('---\nname: alpha\ndescription: 甲\n正文')).toBeNull();
  });

  it('正常的两行 → {name, description}；缺 description 给空串而不是 undefined', () => {
    expect(readSkillFrontmatter('---\nname: alpha\ndescription: 甲\n---\n正文')).toEqual({
      name: 'alpha',
      description: '甲',
    });
    // 缺的字段必须是 ''：RepairBanner 靠 !fm.description.trim() 判断要不要补
    expect(readSkillFrontmatter('---\nname: alpha\n---\n正文')).toEqual({
      name: 'alpha',
      description: '',
    });
  });

  it('双引号 / 单引号包裹的值要去引号（引号是 YAML 外壳，不是值的一部分）', () => {
    expect(
      readSkillFrontmatter('---\nname: "alpha"\ndescription: \'带 : 冒号的说明\'\n---\n正文'),
    ).toEqual({ name: 'alpha', description: '带 : 冒号的说明' });
  });

  it('field: value 之外的行忽略，正文里的 --- 不会当成结束之外的东西', () => {
    expect(
      readSkillFrontmatter('---\nname: alpha\ntags: [a, b]\ndescription: 甲\n---\n正文'),
    ).toEqual({ name: 'alpha', description: '甲' });
  });
});

describe('repairSkillContent —— 只补 frontmatter，绝不重排正文', () => {
  it('完全没有 frontmatter → 开头补出合法一段，原文一字不动地接在后面', () => {
    const out = repairSkillContent('# 我的 skill\n\n步骤一。', 'my-skill');
    // 补完的结果要能被同一个读取器认出来（两个函数互为对照）
    expect(readSkillFrontmatter(out)).toEqual({
      name: 'my-skill',
      description: expect.stringContaining('my-skill'),
    });
    // 正文必须原样留着：用户的正文才是他写这个 skill 的目的
    expect(out).toContain('# 我的 skill\n\n步骤一。');
  });

  it('有 frontmatter 但缺 name → 只插一行 name（取目录名），正文与其余行不重排', () => {
    const src = '---\ndescription: 甲\n---\n\n# 正文\n第二行';
    expect(repairSkillContent(src, 'my-skill')).toBe(
      '---\ndescription: 甲\nname: my-skill\n---\n\n# 正文\n第二行',
    );
  });

  it('有 frontmatter 但缺 description → 只补 description，已有的 name 不被目录名顶替', () => {
    const out = repairSkillContent('---\nname: other\n---\n正文', 'my-skill');
    expect(readSkillFrontmatter(out)?.name).toBe('other');
    expect(readSkillFrontmatter(out)?.description).toBeTruthy();
    // 正文还在末尾，没被吃掉也没被挪到前面
    expect(out.endsWith('\n正文')).toBe(true);
  });

  it('两项都在 → 原样返回（幂等，不会塞出第二段 frontmatter）', () => {
    const good = '---\nname: alpha\ndescription: 甲\n---\n\n# 正文';
    expect(repairSkillContent(good, 'alpha')).toBe(good);
  });

  it('repairSkillContent(repairSkillContent(x)) 幂等：修完的 skill 再点一次补全不会变动', () => {
    const cases = [
      '# 无 frontmatter 的正文',
      '---\ndescription: 甲\n---\n正文',
      '---\nname: x\n---\n正文',
      '---\nname: "\n---\n正文',
    ];
    for (const src of cases) {
      const once = repairSkillContent(src, 'dir-name');
      expect(repairSkillContent(once, 'dir-name')).toBe(once);
      expect(readSkillFrontmatter(once)).not.toBeNull();
    }
  });
});
