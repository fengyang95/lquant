'use client';

import { useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';

import type { AgentCapabilities } from '@/lib/agent-api';
import {
  PROVIDER_LABELS,
  SKILL_NAME_RE,
  getCapabilities,
  putSkill,
  renderSkillTemplate,
  selectableSkills,
} from '@/lib/agent-api';

/** 勾选/取消一个名字（保持原有顺序，便于对比两次选择） */
export function toggleName(list: string[], name: string): string[] {
  return list.includes(name) ? list.filter((x) => x !== name) : [...list, name];
}

/** 反选：在**可选集合**内取反。
 *
 *  只按 `all` 取反、不碰 `all` 之外已选的名字：那些只可能是后来失效的 skill
 *  （目录被删/改名），把它们顺手清掉等于替用户做了一次他没要求的裁剪。
 *  顺序也跟着 `all` 走，与「全选」的结果一致，两次点击的结果可以直接对比。 */
export function invertSelection(selected: string[], all: string[]): string[] {
  const outside = selected.filter((n) => !all.includes(n));
  return [...all.filter((n) => !selected.includes(n)), ...outside];
}

/**
 * 能力选择器：provider（可锁定只读）+ skill + MCP 工具。
 *
 * 「新建会话」与「会话内改能力」两处共用同一份 —— 两处各写一套的话，
 * 「全选到底包不包含坏 skill」这类口径必然分叉（本仓反复踩过）。
 */
export default function CapabilityPicker({
  caps,
  onCapsChange,
  provider,
  onProviderChange,
  providerLocked = false,
  skills,
  onSkillsChange,
  tools,
  onToolsChange,
}: {
  caps: AgentCapabilities;
  /** 新建 skill 成功后交回刷新过的新清单 */
  onCapsChange: (caps: AgentCapabilities) => void;
  provider: string;
  onProviderChange?: (id: string) => void;
  /** true = provider 只读（会话内改能力时） */
  providerLocked?: boolean;
  skills: string[];
  /** 用 setState 的 updater 形态：新建 skill 是异步的，等着等着用户可能又勾了
   *  别的项，用渲染期捕获的旧快照做 toggle 会把那次勾选吞掉。 */
  onSkillsChange: Dispatch<SetStateAction<string[]>>;
  tools: string[];
  onToolsChange: Dispatch<SetStateAction<string[]>>;
}) {
  const [creating, setCreating] = useState(false);
  const usable = selectableSkills(caps);

  return (
    <div className="space-y-4">
      <section>
        <div className="mb-1.5 text-xs font-medium text-ink-dim">能力后端</div>
        {providerLocked ? (
          <div className="text-sm text-ink">
            {PROVIDER_LABELS[provider] ?? (provider || '默认后端')}
            <span className="ml-2 text-xs text-ink-faint">
              建会话时锁定 —— 换后端会续到另一个 CLI 的会话
            </span>
          </div>
        ) : (
          <div className="space-y-1">
            {caps.providers.map((p) => (
              <label key={p.id} className="flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="agent-provider"
                  value={p.id}
                  checked={provider === p.id}
                  onChange={() => onProviderChange?.(p.id)}
                />
                <span className="text-ink">{p.label}</span>
                {!p.available ? (
                  <span className="text-xs text-ink-faint">（本机未检测到该 CLI）</span>
                ) : null}
              </label>
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="text-xs font-medium text-ink-dim">
            Skill
            <span className="ml-1.5 text-ink-faint">
              已选 {skills.length}/{usable.length}
            </span>
          </span>
          <span className="flex gap-2 text-xs">
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => setCreating((v) => !v)}
            >
              {creating ? '取消新建' : '＋ 新建 skill'}
            </button>
            <a href="/settings#skills" className="text-ink-dim hover:text-up">
              skill 管理 ↗
            </a>
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => onSkillsChange(usable)}
            >
              全选
            </button>
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => onSkillsChange((prev) => invertSelection(prev, usable))}
            >
              反选
            </button>
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => onSkillsChange([])}
            >
              清空
            </button>
          </span>
        </div>
        {creating ? (
          <NewSkillForm
            caps={caps}
            onCreated={(name, next) => {
              onCapsChange(next);
              onSkillsChange((prev) => toggleName(prev, name));
              setCreating(false);
            }}
          />
        ) : null}
        <NameList
          items={caps.skills.map((s) => ({
            name: s.name,
            hint: s.valid ? s.description : '名称或 frontmatter 不合格，无法启用 —— 去设置页修',
            disabled: !s.valid,
          }))}
          selected={skills}
          onToggle={(n) => onSkillsChange((prev) => toggleName(prev, n))}
          searchPlaceholder="搜索 skill"
        />
      </section>

      <section>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="text-xs font-medium text-ink-dim">
            MCP 工具
            <span className="ml-1.5 text-ink-faint">
              已选 {tools.length}/{caps.mcp_tools.length}
            </span>
          </span>
          <span className="flex gap-2 text-xs">
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => onToolsChange(caps.mcp_tools.map((t) => t.name))}
            >
              全选
            </button>
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() =>
                onToolsChange((prev) =>
                  invertSelection(prev, caps.mcp_tools.map((t) => t.name)),
                )
              }
            >
              反选
            </button>
            <button
              type="button"
              className="text-ink-dim hover:text-up"
              onClick={() => onToolsChange([])}
            >
              清空
            </button>
          </span>
        </div>
        <NameList
          items={caps.mcp_tools.map((t) => ({ name: t.name, hint: t.description }))}
          selected={tools}
          onToggle={(n) => onToolsChange((prev) => toggleName(prev, n))}
          searchPlaceholder="搜索 MCP 工具"
        />
      </section>
    </div>
  );
}

/** 名字清单 + 搜索框。
 *
 *  搜索只过滤**显示**，不影响「全选 / 反选 / 清空」的作用面：那三个按钮按
 *  完整清单算。反过来的话，用户搜「limit」后点全选会得到一个只含 limit 的
 *  名单，而界面上根本看不出自己丢掉了别的项 —— 这类「操作面随视图变」的
 *  行为是最容易静默出错的。
 */
function NameList({
  items,
  selected,
  onToggle,
  searchPlaceholder,
}: {
  items: { name: string; hint: string; disabled?: boolean }[];
  selected: string[];
  onToggle: (name: string) => void;
  searchPlaceholder: string;
}) {
  const [query, setQuery] = useState('');
  if (items.length === 0) {
    return (
      <div className="border border-dashed border-line px-3 py-4 text-center text-xs text-ink-faint">
        暂无可选项
      </div>
    );
  }
  const q = query.trim().toLowerCase();
  const shown = q ? items.filter((it) => it.name.toLowerCase().includes(q)) : items;
  return (
    <div>
      {items.length > 6 ? (
        <input
          className="input mb-1 w-full text-xs"
          placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      ) : null}
      {shown.length === 0 ? (
        <div className="border border-dashed border-line px-3 py-3 text-center text-xs text-ink-faint">
          没有匹配「{query.trim()}」的项
        </div>
      ) : (
        <ul className="max-h-44 space-y-0.5 overflow-y-auto border border-line bg-white p-2">
          {shown.map((it) => (
            <li key={it.name}>
              <label
                className={`flex items-start gap-2 py-0.5 text-sm ${
                  it.disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'
                }`}
              >
                <input
                  type="checkbox"
                  className="mt-1"
                  disabled={it.disabled}
                  checked={selected.includes(it.name)}
                  onChange={() => onToggle(it.name)}
                />
                <span className="min-w-0">
                  <span className="font-mono text-xs text-ink">{it.name}</span>
                  <span className="ml-2 text-xs text-ink-faint">{it.hint}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * 就地新建 skill：名字 + 一句话描述 → 生成带 frontmatter 的 SKILL.md。
 *
 * 只需要两项输入，是因为 `capabilities.validate_skill_content` 也只认这两项：
 * 少了它们，skill 在能力清单与 Agent Card 里等于不存在。正文骨架先给模板，
 * 想细写再去设置页的编辑器（这里不重复造一个富文本编辑器）。
 */
function NewSkillForm({
  caps,
  onCreated,
}: {
  caps: AgentCapabilities;
  onCreated: (name: string, next: AgentCapabilities) => void;
}) {
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async () => {
    const n = name.trim();
    if (!SKILL_NAME_RE.test(n)) {
      setErr('名字只能用小写字母、数字与短横线，且以字母或数字开头');
      return;
    }
    if (!desc.trim()) {
      setErr('写一句描述 —— agent 靠它判断什么时候该用这个 skill');
      return;
    }
    if (caps.skills.some((s) => s.name === n)) {
      setErr(`已存在同名 skill：${n}`);
      return;
    }
    setBusy(true);
    setErr('');
    try {
      await putSkill(n, renderSkillTemplate(n, desc));
      const next = await getCapabilities();
      setName('');
      setDesc('');
      onCreated(n, next);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-2 space-y-1.5 border border-line bg-white p-2">
      <input
        className="input w-full font-mono text-xs"
        placeholder="新 skill 名（如 limit-up-scan）"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <input
        className="input w-full text-xs"
        placeholder="一句话描述：它解决什么问题、什么时候该用"
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void submit();
        }}
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn btn-sm btn-primary"
          disabled={busy}
          onClick={() => void submit()}
        >
          {busy ? '创建中…' : '创建并勾选'}
        </button>
        <span className="text-[11px] text-ink-faint">
          正文先给模板，细节去设置页编辑
        </span>
      </div>
      {err ? <div className="text-xs text-up">{err}</div> : null}
    </div>
  );
}
