import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  AGENT_SETTING_KEYS,
  isOverridden,
  listSettings,
  putSetting,
  resetSetting,
  settingBool,
  settingText,
  type SettingItem,
} from './settings-api';
import { stubPageFetch } from '@/test/page-utils';

const item = (over: Partial<SettingItem> = {}): SettingItem => ({
  key: 'agent.provider',
  value: 'claude_code',
  type: 'enum',
  source: 'default',
  label: '问 AI 后端',
  choices: null,
  ...over,
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AGENT_SETTING_KEYS', () => {
  it('覆盖问 AI 面板用到的六项，key 与后端白名单同名', () => {
    // 面板固定依赖下面六项；白名单以后新增项不应打断这里
    expect(AGENT_SETTING_KEYS).toMatchObject({
      provider: 'agent.provider',
      defaultSkills: 'agent.default_skills',
      defaultMcpTools: 'agent.default_mcp_tools',
      timeout: 'agent.timeout_seconds',
      skipPermissions: 'agent.skip_permissions',
      partialMessages: 'agent.partial_messages',
    });
    // 这份白名单只装 agent.* 项，不能夹带全局配置（settings 页另有一套）
    expect(Object.values(AGENT_SETTING_KEYS).every((k) => k.startsWith('agent.'))).toBe(true);
  });
});

describe('settings-api 请求封装', () => {
  it('listSettings 命中 /api/settings 并解出封套里的 list', async () => {
    const rows = [
      item({ key: 'agent.provider', value: 'codex' }),
      item({ key: 'agent.skip_permissions', value: 'true', type: 'bool', source: 'runtime' }),
    ];
    const fetchMock = stubPageFetch({ '/settings': { code: 0, data: rows, message: 'ok' } });

    await expect(listSettings()).resolves.toEqual(rows);
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/settings');
  });

  it('putSetting：PUT /settings/{key}，body 同时带 key 与 value', async () => {
    const fetchMock = stubPageFetch({
      '/settings/agent.timeout_seconds': {
        code: 0,
        data: { key: 'agent.timeout_seconds', value: '600' },
        message: 'ok',
      },
    });

    await expect(putSetting('agent.timeout_seconds', '600')).resolves.toEqual({
      key: 'agent.timeout_seconds',
      value: '600',
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('/api/settings/agent.timeout_seconds');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({
      key: 'agent.timeout_seconds',
      value: '600',
    });
  });

  it('resetSetting：DELETE /settings/{key}，不带请求体', async () => {
    const fetchMock = stubPageFetch({
      '/settings/agent.skip_permissions': {
        code: 0,
        data: { key: 'agent.skip_permissions', reset: true },
        message: 'ok',
      },
    });

    await expect(resetSetting('agent.skip_permissions')).resolves.toEqual({
      key: 'agent.skip_permissions',
      reset: true,
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('/api/settings/agent.skip_permissions');
    expect(init.method).toBe('DELETE');
    expect(init.body).toBeUndefined();
  });

  it('key 做 URL 编码：特殊字符不会注入到路径里（PUT 与 DELETE 一致）', async () => {
    const fetchMock = stubPageFetch({
      '/settings/': { code: 0, data: { key: 'a b/c', value: '1' }, message: 'ok' },
    });

    await putSetting('a b/c', '1');
    await resetSetting('a b/c');

    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls).toHaveLength(2);
    expect(urls.every((u) => u.includes('a%20b%2Fc'))).toBe(true);
    expect(urls.some((u) => u.endsWith('/settings/a%20b%2Fc'))).toBe(true);
  });

  it('封套 code!=0 抛错并带上后端 message（非法值 422 由后端文案解释）', async () => {
    stubPageFetch({
      '/settings/agent.timeout_seconds': {
        code: 1,
        data: null,
        message: '超时应在 10~3600 秒之间，收到 5',
      },
    });
    await expect(putSetting('agent.timeout_seconds', '5')).rejects.toThrow(
      '超时应在 10~3600 秒之间，收到 5',
    );
  });
});

describe('settingText —— 取值转表单字符串', () => {
  it('列表为空 / 命中不到 key 都给空串', () => {
    expect(settingText(undefined, 'agent.provider')).toBe('');
    expect(settingText([], 'agent.provider')).toBe('');
    expect(settingText([item()], 'agent.missing')).toBe('');
  });

  it('null / undefined 值给空串，不会显示成 "null"', () => {
    expect(settingText([item({ value: null })], 'agent.provider')).toBe('');
    expect(settingText([item({ value: undefined })], 'agent.provider')).toBe('');
  });

  it('0 / false 这类假值原样转字符串（当作「有值」，不能掉进缺省分支）', () => {
    expect(settingText([item({ value: 0 })], 'agent.provider')).toBe('0');
    expect(settingText([item({ value: false })], 'agent.provider')).toBe('false');
  });

  it('字符串原样返回', () => {
    expect(settingText([item({ value: 'all' })], 'agent.provider')).toBe('all');
  });
});

describe('isOverridden —— 只看 source=runtime', () => {
  it('runtime → true；default / config / 缺项 → false', () => {
    expect(isOverridden([item({ source: 'runtime' })], 'agent.provider')).toBe(true);
    expect(isOverridden([item({ source: 'config' })], 'agent.provider')).toBe(false);
    expect(isOverridden([item({ source: 'default' })], 'agent.provider')).toBe(false);
    expect(isOverridden([item({ source: 'runtime', key: 'other' })], 'agent.provider')).toBe(false);
    expect(isOverridden(undefined, 'agent.provider')).toBe(false);
  });
});

describe('settingBool —— 后端存字符串 "true"/"false"', () => {
  const KEY = 'agent.skip_permissions';
  const bool = (value: unknown) => [item({ key: KEY, value, type: 'bool' })];

  it('缺失 / null / 空串走 fallback', () => {
    expect(settingBool(undefined, KEY, true)).toBe(true);
    expect(settingBool([], KEY, false)).toBe(false);
    expect(settingBool(bool(null), KEY, true)).toBe(true);
    expect(settingBool(bool('  '), KEY, true)).toBe(true);
  });

  it('大小写与空白无关：只有 "true" 为真', () => {
    expect(settingBool(bool('true'), KEY, false)).toBe(true);
    expect(settingBool(bool(' TRUE '), KEY, false)).toBe(true);
    expect(settingBool(bool('false'), KEY, true)).toBe(false);
    expect(settingBool(bool('yes'), KEY, true)).toBe(false);
  });

  it('后端若回的是布尔字面量也能解析', () => {
    expect(settingBool(bool(true), KEY, false)).toBe(true);
    expect(settingBool(bool(false), KEY, true)).toBe(false);
  });
});
