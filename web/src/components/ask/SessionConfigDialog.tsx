'use client';

import { useEffect, useState } from 'react';

import type { AgentCapabilities } from '@/lib/agent-api';
import { encodeSelection, getCapabilities, selectableSkills } from '@/lib/agent-api';
import type { AskSession } from '@/lib/ask-api';
import { updateSessionConfig } from '@/lib/ask-api';
import CapabilityPicker from './CapabilityPicker';

/**
 * 会话内改配置：skill / MCP 工具 / 运行参数（超时、全自主权限）可改，provider 只读。
 *
 * 保存后**下一轮生效**（工作区每轮按会话配置重建、权限与超时每次运行解析，见
 * `CliAgentService._workspace_for` / `_run`），所以不用重开会话、也不用等当前
 * 这轮跑完。能力清单每次打开重新拉：设置页可能刚加了新 skill。
 *
 * 运行参数为什么做成「三态」（跟随默认 / 明确开 / 明确关）而不是一个开关：
 * 会话级的值是**覆盖**，`null` = 回到全局默认档，与「明确设成 false」不是
 * 一回事 —— 用一个 checkbox 表达不出「没设过」，用户会以为自己改了全局值。
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
  const [timeoutText, setTimeoutText] = useState('');
  //: 'default' = 不覆盖（跟随全局）；'on' / 'off' = 本会话明确设定
  const [perms, setPerms] = useState<'default' | 'on' | 'off'>('default');
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
        setTimeoutText(cfg.timeout_seconds == null ? '' : String(cfg.timeout_seconds));
        setPerms(
          cfg.skip_permissions == null ? 'default' : cfg.skip_permissions ? 'on' : 'off',
        );
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
    const raw = timeoutText.trim();
    if (raw !== '' && !/^\d+$/.test(raw)) {
      setErr('超时必须是整数秒（留空 = 跟随全局默认）');
      return;
    }
    setBusy(true);
    setErr('');
    try {
      const updated = await updateSessionConfig(session.id, {
        skills: encodeSelection(skills, selectableSkills(caps)),
        mcp_tools: encodeSelection(tools, caps.mcp_tools.map((t) => t.name)),
        // 空串 → null = 不覆盖，**不是** 0 也不是「关掉」
        timeout_seconds: raw === '' ? null : Number(raw),
        skip_permissions: perms === 'default' ? null : perms === 'on',
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
          <h2 className="text-[13px] font-semibold text-ink">会话能力与运行参数</h2>
          <p className="mt-0.5 text-xs text-ink-faint">
            skill 与 MCP 工具改完下一轮生效；超时与权限下一次运行生效；后端在建会话时锁定
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
            <div className="space-y-5">
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

              <section className="space-y-1.5 border-t border-line pt-3">
                <div className="text-xs font-medium text-ink-dim">本会话运行参数</div>
                <div className="flex items-center gap-2">
                  <input
                    className="input w-28"
                    inputMode="numeric"
                    placeholder="跟随默认"
                    value={timeoutText}
                    onChange={(e) => setTimeoutText(e.target.value)}
                  />
                  <span className="text-xs text-ink-faint">
                    秒（10~3600，留空 = 跟随全局默认）
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <span className="text-ink-dim">全自主权限</span>
                  {([
                    ['default', '跟随默认'],
                    ['on', '本会话开启'],
                    ['off', '本会话关闭'],
                  ] as const).map(([v, label]) => (
                    <label key={v} className="flex items-center gap-1">
                      <input
                        type="radio"
                        name="session-perms"
                        checked={perms === v}
                        onChange={() => setPerms(v)}
                      />
                      <span className={v === 'on' ? 'text-up' : ''}>{label}</span>
                    </label>
                  ))}
                </div>
                <p className="text-[11px] text-ink-faint">
                  「跟随默认」不是「关闭」：它表示本会话不覆盖全局值（全局值在
                  「⚙ AI 设置 → 运行参数」里）。关闭全自主权限后，codex 侧 MCP
                  工具会被审批策略直接拒绝。
                </p>
              </section>
            </div>
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
