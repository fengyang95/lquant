import { delData, getData, putData } from './api';

/** 运行时配置项（后端 /api/settings 的封套 data） */
export interface SettingItem {
  key: string;
  value: unknown;
  type: 'str' | 'bool' | 'int' | 'enum' | 'list';
  source: 'default' | 'config' | 'runtime';
  label: string;
  choices: string[] | null;
}

/** 问 AI 相关配置项的 key（面板与设置页共用同一套后端白名单） */
export const AGENT_SETTING_KEYS = {
  provider: 'agent.provider',
  defaultSkills: 'agent.default_skills',
  defaultMcpTools: 'agent.default_mcp_tools',
  timeout: 'agent.timeout_seconds',
  skipPermissions: 'agent.skip_permissions',
  partialMessages: 'agent.partial_messages',
  maxConcurrentRuns: 'agent.max_concurrent_runs',
} as const;

export function listSettings(): Promise<SettingItem[]> {
  return getData<SettingItem[]>('/settings');
}

/** 写一项运行时配置。非法值后端回 422，message 里是可直接展示的中文原因。 */
export function putSetting(key: string, value: string): Promise<{ key: string; value: unknown }> {
  return putData<{ key: string; value: unknown }>(
    `/settings/${encodeURIComponent(key)}`, { key, value });
}

/** 清掉覆盖值，回到 config/app.yaml 的派生值。 */
export function resetSetting(key: string): Promise<{ key: string; reset: boolean }> {
  return delData<{ key: string; reset: boolean }>(`/settings/${encodeURIComponent(key)}`);
}

/** 设置值 → 表单用的字符串（bool / int / 字符串一视同仁；缺省给空串）。 */
export function settingText(items: SettingItem[] | undefined, key: string): string {
  const it = items?.find((i) => i.key === key);
  if (!it || it.value === null || it.value === undefined) return '';
  return String(it.value);
}

/** 该配置项的当前值是不是来自用户覆盖（source=runtime）。 */
export function isOverridden(items: SettingItem[] | undefined, key: string): boolean {
  return items?.find((i) => i.key === key)?.source === 'runtime';
}

/** bool 配置项 → 复选框中转（后端存的是字符串 "true"/"false"）。 */
export function settingBool(items: SettingItem[] | undefined, key: string, fallback: boolean): boolean {
  const txt = settingText(items, key).trim().toLowerCase();
  if (!txt) return fallback;
  return txt === 'true';
}
