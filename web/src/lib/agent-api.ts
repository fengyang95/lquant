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

/** 勾选集 → 落库值：**全选 = `null`（不裁剪）**。
 *
 *  `null` 与「显式列出全部」当下等价，但语义不同：`null` 是不裁剪，
 *  之后新增的 skill / 工具会自动带上；显式列表则冻结成当下的快照。
 *  界面上的「全选」字面意思就是前者，所以这里回写成 `null`。
 *  可选项为空时同样回写 `null`：全选一个空集仍是「不裁剪」，写成 `[]`
 *  会把它降级成「一个都不启用」，以后新增的能力就不会自动带上了。 */
export function encodeSelection(selected: string[], all: string[]): string[] | null {
  if (all.every((n) => selected.includes(n))) return null;
  return selected;
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

/** skill 名形状，与后端 `capabilities.SKILL_NAME_RE` 同一口径。
 *  前端先挡一道是为了给即时反馈；真正的门禁仍在后端（写接口会 400）。 */
export const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

/** 按名字 + 一句话描述渲染 skill 骨架（新建向导用，省去手写 frontmatter）。
 *
 *  两个坑：
 *  1. description 直接进 YAML frontmatter，**先用双引号包裹再转义** `\` 与 `"`；
 *     含换行或 `: ` 会把 frontmatter 写坏，而坏掉的 skill 在能力清单里是
 *     valid=false，用户只会看到「新建成功但选不了」。
 *  2. 替换串必须用**函数形式**：字符串形式的 `$&` / `` $` `` / `$'` 会被
 *     `String.replace` 当成替换模式解释，用户描述里带一个 `$&` 就会把模板原文
 *     插进 frontmatter（静默写出坏 skill）。 */
export function renderSkillTemplate(name: string, description: string): string {
  const desc = description
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
  return NEW_SKILL_TEMPLATE
    .replace(/^name: my-skill$/m, () => `name: ${name}`)
    .replace(/^description: .*$/m, () => `description: "${desc}"`)
    // 正文标题也跟着改：留着 my-skill 会让人以为 skill 没建对
    .replace(/^# my-skill$/m, () => `# ${name}`);
}
