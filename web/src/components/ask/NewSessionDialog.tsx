'use client';

import { useState } from 'react';

import type { AgentCapabilities, AgentConfig } from '@/lib/agent-api';
import { resolveDefaults, selectableSkills } from '@/lib/agent-api';

function toggle(list: string[], name: string): string[] {
  return list.includes(name) ? list.filter((x) => x !== name) : [...list, name];
}

/**
 * 新建会话弹层：选定本次会话的能力后端与能力集。
 *
 * 三项在**建会话时锁定、建后不可改**（provider 决定 CLI 侧会话 id 的口径，
 * 中途换后端续接的就是别人的会话），所以这里显式说明而不是做成会话内的开关。
 */
export default function NewSessionDialog({
  caps,
  busy = false,
  error = null,
  onCancel,
  onCreate,
}: {
  caps: AgentCapabilities;
  busy?: boolean;
  error?: string | null;
  onCancel: () => void;
  onCreate: (cfg: AgentConfig) => void;
}) {
  const init = resolveDefaults(caps);
  const [provider, setProvider] = useState(init.provider);
  const [skills, setSkills] = useState<string[]>(init.skills);
  const [tools, setTools] = useState<string[]>(init.mcpTools);

  const submit = () => {
    if (busy || !provider) return;
    onCreate({ provider, skills, mcp_tools: tools });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/20 p-4">
      <div
        role="dialog"
        aria-label="新建会话"
        className="flex max-h-[85vh] w-[560px] max-w-full flex-col border border-line-strong bg-paper"
      >
        <header className="border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-ink">新建会话</h2>
          <p className="mt-0.5 text-xs text-ink-faint">
            能力后端与能力集在建会话时锁定，之后不可修改
          </p>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          <section>
            <div className="mb-1.5 text-xs font-medium text-ink-dim">能力后端</div>
            <div className="space-y-1">
              {caps.providers.map((p) => (
                <label key={p.id} className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="agent-provider"
                    value={p.id}
                    checked={provider === p.id}
                    onChange={() => setProvider(p.id)}
                  />
                  <span className="text-ink">{p.label}</span>
                  {!p.available ? (
                    <span className="text-xs text-ink-faint">（本机未检测到该 CLI）</span>
                  ) : null}
                </label>
              ))}
            </div>
          </section>

          <CapabilityGroup
            title="Skill"
            items={caps.skills.map((s) => ({
              name: s.name,
              hint: s.valid ? s.description : '名称或 frontmatter 不合格，无法启用',
              disabled: !s.valid,
            }))}
            selected={skills}
            onToggle={(n) => setSkills((prev) => toggle(prev, n))}
            onAll={() => setSkills(selectableSkills(caps))}
            onNone={() => setSkills([])}
          />

          <CapabilityGroup
            title="MCP 工具"
            items={caps.mcp_tools.map((t) => ({ name: t.name, hint: t.description }))}
            selected={tools}
            onToggle={(n) => setTools((prev) => toggle(prev, n))}
            onAll={() => setTools(caps.mcp_tools.map((t) => t.name))}
            onNone={() => setTools([])}
          />
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-4 py-2.5">
          <span className="min-w-0 flex-1 truncate text-xs text-up">{error}</span>
          <div className="flex shrink-0 gap-2">
            <button className="btn" onClick={onCancel} disabled={busy}>
              取消
            </button>
            <button className="btn btn-primary" onClick={submit} disabled={busy || !provider}>
              {busy ? '创建中…' : '创建会话'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

function CapabilityGroup({
  title,
  items,
  selected,
  onToggle,
  onAll,
  onNone,
}: {
  title: string;
  items: { name: string; hint: string; disabled?: boolean }[];
  selected: string[];
  onToggle: (name: string) => void;
  onAll: () => void;
  onNone: () => void;
}) {
  return (
    <section>
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-xs font-medium text-ink-dim">
          {title}
          <span className="ml-1.5 text-ink-faint">
            已选 {selected.length}/{items.length}
          </span>
        </span>
        <span className="flex gap-2 text-xs">
          <button type="button" className="text-ink-dim hover:text-up" onClick={onAll}>
            全选
          </button>
          <button type="button" className="text-ink-dim hover:text-up" onClick={onNone}>
            清空
          </button>
        </span>
      </div>
      {items.length === 0 ? (
        <div className="border border-dashed border-line px-3 py-4 text-center text-xs text-ink-faint">
          暂无可选项
        </div>
      ) : (
        <ul className="max-h-44 space-y-0.5 overflow-y-auto border border-line bg-white p-2">
          {items.map((it) => (
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
    </section>
  );
}
