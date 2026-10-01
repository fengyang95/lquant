import { delData, getData, putData } from './api';

/** 可选的能力后端（CLI provider） */
export interface AgentProvider {
  id: string;
  label: string;
  /** CLI 是否在本机 PATH 上；仅作提示，不阻止选择 */
  available: boolean;
}

/** config/skills/ 下的一个 skill；valid=false 表示 frontmatter 不合格 */
export interface AgentSkill {
  name: string;
  description: string;
  tags: string[];
  valid: boolean;
}

export interface AgentMcpTool {
  name: string;
  description: string;
}

/**
 * 会话级能力配置。`null` / 缺省 = 不裁剪（全开）；`[]` = 一个都不启用。
 * 两者语义不同，不要合并成同一个空值。
 */
export interface AgentConfig {
  provider?: string | null;
  skills?: string[] | null;
  mcp_tools?: string[] | null;
}

export interface AgentCapabilities {
  providers: AgentProvider[];
  skills: AgentSkill[];
  mcp_tools: AgentMcpTool[];
  defaults: AgentConfig;
}

const BASE = '/agent';

/** provider 显示名的本地兜底（正常走 /capabilities 的 label，这里用于
 *  会话头部这种只有 id 的场景）。未知 id 原样显示，不吞掉。 */
export const PROVIDER_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
  mock: 'Mock',
};

export function getCapabilities(): Promise<AgentCapabilities> {
  return getData<AgentCapabilities>(`${BASE}/capabilities`);
}

export function getSkill(name: string): Promise<{ name: string; content: string }> {
  return getData<{ name: string; content: string }>(
    `${BASE}/skills/${encodeURIComponent(name)}`,
  );
}

export function putSkill(name: string, content: string): Promise<{ ok: boolean }> {
  return putData<{ ok: boolean }>(`${BASE}/skills/${encodeURIComponent(name)}`, { content });
}

export function deleteSkill(name: string): Promise<{ ok: boolean }> {
  return delData<{ ok: boolean }>(`${BASE}/skills/${encodeURIComponent(name)}`);
}

/** 可勾选的 skill：`valid=false` 的（名字或 frontmatter 不合格）选不了 ——
 *  后端会拒收，勾上只会让建会话 400。 */
export function selectableSkills(caps: AgentCapabilities): string[] {
  return caps.skills.filter((s) => s.valid).map((s) => s.name);
}

/** 新建会话弹层的初始勾选：defaults 里的 null = 全开 → 全部可选的都勾上 */
export function resolveDefaults(caps: AgentCapabilities): {
  provider: string;
  skills: string[];
  mcpTools: string[];
} {
  return {
    provider: caps.defaults.provider ?? caps.providers[0]?.id ?? '',
    skills: caps.defaults.skills ?? selectableSkills(caps),
    mcpTools: caps.defaults.mcp_tools ?? caps.mcp_tools.map((t) => t.name),
  };
}

export const NEW_SKILL_TEMPLATE = `---
name: my-skill
description: 一句话说明这个 skill 解决什么问题、什么时候该用它
tags: []
---

# my-skill

在这里写清步骤与口径：先查什么、走哪个接口、结论怎么给。
`;
