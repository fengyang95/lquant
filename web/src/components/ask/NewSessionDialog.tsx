'use client';

import { useState } from 'react';

import type { AgentCapabilities, AgentConfig } from '@/lib/agent-api';
import { encodeSelection, resolveDefaults, selectableSkills } from '@/lib/agent-api';
import CapabilityPicker from './CapabilityPicker';

/**
 * 新建会话弹层：选定本次会话的能力后端与能力集。
 *
 * provider 建会话时锁定（它决定 CLI 侧会话 id 的口径，中途换后端续接的
 * 就是别人的会话）；skill / MCP 工具之后还能在会话里改（见
 * SessionConfigDialog）—— 所以这里只把 provider 说成不可改，别吓唬用户。
 */
export default function NewSessionDialog({
  caps: initialCaps,
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
  const init = resolveDefaults(initialCaps);
  // 弹层内新建 skill 后清单会变（新 skill 要立刻出现在列表里），所以自己持有一份
  const [caps, setCaps] = useState(initialCaps);
  const [provider, setProvider] = useState(init.provider);
  const [skills, setSkills] = useState<string[]>(init.skills);
  const [tools, setTools] = useState<string[]>(init.mcpTools);

  const submit = () => {
    if (busy || !provider) return;
    // 与「会话内改能力」同一口径：全选回写成 null（不裁剪），以后新增的
    // skill / 工具会自动带上；只有真正取消勾选过才落成显式名单。
    onCreate({
      provider,
      skills: encodeSelection(skills, selectableSkills(caps)),
      mcp_tools: encodeSelection(tools, caps.mcp_tools.map((t) => t.name)),
    });
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
            能力后端建会话时锁定；skill 与 MCP 工具之后仍可在会话里调整
          </p>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <CapabilityPicker
            caps={caps}
            onCapsChange={setCaps}
            provider={provider}
            onProviderChange={setProvider}
            skills={skills}
            onSkillsChange={setSkills}
            tools={tools}
            onToolsChange={setTools}
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
