'use client';

import { useEffect, useState } from 'react';

import type { AgentCapabilities } from '@/lib/agent-api';
import { encodeSelection, getCapabilities, selectableSkills } from '@/lib/agent-api';
import type { AskSession } from '@/lib/ask-api';
import { updateSessionConfig } from '@/lib/ask-api';
import CapabilityPicker from './CapabilityPicker';

/**
 * 会话内改能力：skill 与 MCP 工具可改，provider 只读。
 *
 * 保存后**下一轮生效**（工作区每轮按会话配置重建，见
 * `CliAgentService._workspace_for`），所以不用重开会话、也不用等当前这轮跑完。
 * 能力清单每次打开重新拉：设置页可能刚加了新 skill。
 */
export default function SessionConfigDialog({
  session,
  onCancel,
  onSaved,
}: {
  session: AskSession;
  onCancel: () => void;
  onSaved: (updated: AskSession) => void;
}) {
  const [caps, setCaps] = useState<AgentCapabilities | null>(null);
  const [loadError, setLoadError] = useState('');
  const [skills, setSkills] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    let alive = true;
    getCapabilities()
      .then((c) => {
        if (!alive) return;
        const cfg = session.agent_config ?? {};
        setCaps(c);
        // null = 不裁剪：按「全选」预填，与创建时看到的勾选一致
        setSkills(cfg.skills ?? selectableSkills(c));
        setTools(cfg.mcp_tools ?? c.mcp_tools.map((t) => t.name));
      })
      .catch((e) => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [session]);

  const save = async () => {
    if (!caps || busy) return;
    setBusy(true);
    setErr('');
    try {
      const updated = await updateSessionConfig(session.id, {
        skills: encodeSelection(skills, selectableSkills(caps)),
        mcp_tools: encodeSelection(tools, caps.mcp_tools.map((t) => t.name)),
      });
      onSaved(updated);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/20 p-4">
      <div
        role="dialog"
        aria-label="会话能力"
        className="flex max-h-[85vh] w-[560px] max-w-full flex-col border border-line-strong bg-paper"
      >
        <header className="border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-ink">会话能力</h2>
          <p className="mt-0.5 text-xs text-ink-faint">
            skill 与 MCP 工具改完下一轮生效；能力后端在建会话时锁定
          </p>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {loadError ? (
            <div className="border-l-2 border-up bg-panel px-3 py-2 text-sm text-up">
              能力清单加载失败：{loadError}
            </div>
          ) : !caps ? (
            <div className="py-8 text-center text-sm text-ink-faint">加载能力清单…</div>
          ) : (
            <CapabilityPicker
              caps={caps}
              onCapsChange={setCaps}
              provider={session.agent_config?.provider ?? ''}
              providerLocked
              skills={skills}
              onSkillsChange={setSkills}
              tools={tools}
              onToolsChange={setTools}
            />
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-4 py-2.5">
          <span className="min-w-0 flex-1 truncate text-xs text-up">{err}</span>
          <div className="flex shrink-0 gap-2">
            <button className="btn" onClick={onCancel} disabled={busy}>
              取消
            </button>
            <button className="btn btn-primary" onClick={() => void save()} disabled={busy || !caps}>
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
