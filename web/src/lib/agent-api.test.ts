import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deleteSkill,
  getCapabilities,
  getSkill,
  putSkill,
  resolveDefaults,
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
