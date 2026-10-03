'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';

import type { AgentCapabilities } from '@/lib/agent-api';
import { encodeSelection, getCapabilities, resolveDefaults, selectableSkills } from '@/lib/agent-api';
import type { SettingItem } from '@/lib/settings-api';
import {
  AGENT_SETTING_KEYS as K,
  isOverridden,
  listSettings,
  putSetting,
  resetSetting,
  settingBool,
  settingText,
} from '@/lib/settings-api';
import SkillEditor from '@/components/settings/SkillEditor';
import CapabilityPicker from './CapabilityPicker';

const TABS = [
  { id: 'capability', label: '默认能力' },
  { id: 'skill', label: 'Skill 编辑' },
  { id: 'runtime', label: '运行参数' },
] as const;
type TabId = (typeof TABS)[number]['id'];

function msgOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 勾选集 → 配置字符串（后端存的是字符串：all / none / 逗号名单）。
 *
 *  全选写成 ``all``（不裁剪）而不是把名字列一遍 —— 以后新增的 skill / 工具
 *  才会自动带上；这与会话内「调整能力」的 ``null`` 是同一条语义。 */
export function capabilityValue(selected: string[], all: string[]): string {
  const enc = encodeSelection(selected, all);
  if (enc === null) return 'all';
  if (enc.length === 0) return 'none';
  return enc.join(',');
}

/**
 * 「问 AI」页内的 AI 设置面板：默认能力（provider / skill / MCP）+ skill 正文
 * 编辑 + 运行参数（超时 / 权限 / 流式），全部**改完立即生效**，不用跳设置页、
 * 不用重启服务。
 *
 * 取值与写入都走 /api/settings 的同一份白名单：面板只是「从问 AI 页打开」，
 * 校验、覆盖层语义、来源标记与设置页完全一致（两处各写一套必然分叉）。
 */
export default function AiSettingsPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<TabId>('capability');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');

  const { data: caps, mutate: mutateCaps } = useSWR<AgentCapabilities>(
    '/agent/capabilities', getCapabilities);
  const { data: settings, mutate: mutateSettings } = useSWR<SettingItem[]>(
    '/settings', listSettings);

  const save = async (key: string, value: string, label: string) => {
    setBusy(key);
    setMsg('');
    try {
      await putSetting(key, value);
      setMsg(`✓ ${label} 已保存`);
      await mutateSettings();
      await mutateCaps(); // 默认能力会影响新建会话的预填（/capabilities 的 defaults）
    } catch (e) {
      setMsg(`✗ ${msgOf(e)}`);
    } finally {
      setBusy('');
    }
  };

  const reset = async (key: string, label: string) => {
    setBusy(key);
    setMsg('');
    try {
      await resetSetting(key);
      setMsg(`✓ ${label} 已重置为配置默认`);
      await mutateSettings();
      await mutateCaps();
    } catch (e) {
      setMsg(`✗ ${msgOf(e)}`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink/20" onClick={onClose}>
      <div
        role="dialog"
        aria-label="AI 设置"
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-[620px] max-w-full flex-col border-l border-line-strong bg-paper"
      >
        <header className="border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-ink">AI 设置</h2>
          <p className="mt-0.5 text-xs text-ink-faint">
            改完立即生效（下一次新建会话 / 下一次运行），不用重启服务
          </p>
        </header>

        <nav className="flex gap-1 border-b border-line px-3 pt-2">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => {
                setTab(t.id);
                setMsg('');
              }}
              className={`border-b-2 px-3 py-1.5 text-sm ${
                tab === t.id
                  ? 'border-up font-medium text-ink'
                  : 'border-transparent text-ink-dim hover:text-ink'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {msg ? (
          <div className={`border-b border-line px-4 py-1.5 text-xs ${
            msg.startsWith('✓') ? 'text-down' : 'text-up'}`}>
            {msg}
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {tab === 'capability' ? (
            <CapabilityDefaults
              caps={caps}
              settings={settings}
              busy={busy}
              onCapsChange={() => {
                void mutateCaps();
              }}
              onSave={save}
              onReset={reset}
            />
          ) : tab === 'skill' ? (
            <div className="space-y-2">
              <p className="text-xs text-ink-faint">
                SKILL.md 原文编辑：frontmatter 的 name / description 必填（缺了这个
                skill 在能力清单与 Agent Card 里都不可见）。保存后下一轮对话即生效。
              </p>
              <SkillEditor />
            </div>
          ) : (
            <RuntimeSettings
              settings={settings}
              busy={busy}
              onSave={save}
              onReset={reset}
            />
          )}
        </div>

        <footer className="flex items-center justify-end gap-3 border-t border-line px-4 py-2.5">
          <span className="mr-auto text-[11px] text-ink-faint">
            覆盖层：代码默认 &lt; config/app.yaml &lt; 此处写入
          </span>
          <button className="btn" onClick={onClose}>
            关闭
          </button>
        </footer>
      </div>
    </div>
  );
}

/** 默认能力：新建会话的预填（provider + skill + MCP 工具）。 */
function CapabilityDefaults({
  caps,
  settings,
  busy,
  onCapsChange,
  onSave,
  onReset,
}: {
  caps?: AgentCapabilities;
  settings?: SettingItem[];
  busy: string;
  onCapsChange: (next?: AgentCapabilities) => void;
  onSave: (key: string, value: string, label: string) => Promise<void>;
  onReset: (key: string, label: string) => Promise<void>;
}) {
  const [local, setLocal] = useState<AgentCapabilities | null>(null);
  const [provider, setProvider] = useState('');
  const [skills, setSkills] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);

  // 能力清单拉回来（或面板里新建 skill 后刷新）时同步一次表单
  const source = local ?? caps;
  useEffect(() => {
    if (!source) return;
    const init = resolveDefaults(source);
    setProvider(init.provider);
    setSkills(init.skills);
    setTools(init.mcpTools);
  }, [source]);
  if (!source) {
    return <div className="py-8 text-center text-sm text-ink-faint">加载能力清单…</div>;
  }
  const usable = selectableSkills(source);
  const allTools = source.mcp_tools.map((t) => t.name);

  const saveAll = async () => {
    await onSave(K.provider, provider, '默认后端');
    await onSave(K.defaultSkills, capabilityValue(skills, usable), '默认 skill');
    await onSave(K.defaultMcpTools, capabilityValue(tools, allTools), '默认 MCP 工具');
  };

  return (
    <div className="space-y-4">
      <p className="text-xs text-ink-faint">
        这三项是**新建会话**时的预填值；会话建好后仍可在会话头的「调整能力」里单独改。
      </p>
      <CapabilityPicker
        caps={source}
        onCapsChange={(next) => {
          setLocal(next);
          onCapsChange(next);
        }}
        provider={provider}
        onProviderChange={setProvider}
        skills={skills}
        onSkillsChange={setSkills}
        tools={tools}
        onToolsChange={setTools}
      />
      <div className="flex items-center gap-2">
        <button
          className="btn btn-primary"
          disabled={!!busy || !provider}
          onClick={() => void saveAll()}
        >
          {busy ? '保存中…' : '保存默认能力'}
        </button>
        <button
          className="btn"
          disabled={!!busy}
          onClick={async () => {
            await onReset(K.defaultSkills, '默认 skill');
            await onReset(K.defaultMcpTools, '默认 MCP 工具');
          }}
        >
          恢复配置默认
        </button>
      </div>
      <p className="text-[11px] text-ink-faint">
        「全选」写成 <code className="font-mono">all</code>（不裁剪）：以后新增的
        skill / 工具会自动带上；「清空」写成 <code className="font-mono">none</code>。
      </p>
      <SourceHint settings={settings} keys={[K.provider, K.defaultSkills, K.defaultMcpTools]} />
    </div>
  );
}

/** 运行参数：超时 / 全自主权限 / token 级流式。 */
function RuntimeSettings({
  settings,
  busy,
  onSave,
  onReset,
}: {
  settings?: SettingItem[];
  busy: string;
  onSave: (key: string, value: string, label: string) => Promise<void>;
  onReset: (key: string, label: string) => Promise<void>;
}) {
  const [timeout, setTimeoutText] = useState('');
  const timeoutValue = settingText(settings, K.timeout);
  useEffect(() => {
    setTimeoutText(timeoutValue);
  }, [timeoutValue]);
  const [maxRuns, setMaxRuns] = useState('');
  const maxRunsValue = settingText(settings, K.maxConcurrentRuns);
  useEffect(() => {
    setMaxRuns(maxRunsValue);
  }, [maxRunsValue]);

  if (!settings || settings.length === 0) {
    return <div className="py-8 text-center text-sm text-ink-faint">加载配置…</div>;
  }
  const skip = settingBool(settings, K.skipPermissions, true);
  const partial = settingBool(settings, K.partialMessages, true);
  const label = (key: string) =>
    settings.find((s) => s.key === key)?.label ?? key;

  return (
    <div className="space-y-5">
      <section className="space-y-1.5">
        <div className="text-xs font-medium text-ink-dim">{label(K.timeout)}</div>
        <div className="flex items-center gap-2">
          <input
            className="input w-28"
            inputMode="numeric"
            value={timeout}
            onChange={(e) => setTimeoutText(e.target.value)}
          />
          <span className="text-xs text-ink-faint">秒（10~3600）</span>
          <button
            className="btn btn-sm"
            aria-label="保存超时"
            disabled={!!busy || timeout === timeoutValue}
            onClick={() => void onSave(K.timeout, timeout, '超时')}
          >
            保存
          </button>
          <Overridden onReset={() => void onReset(K.timeout, '超时')} settings={settings} k={K.timeout} />
        </div>
        <p className="text-[11px] text-ink-faint">
          超时会 kill 掉正在跑的 CLI 子进程。改完**下一次运行**生效（正在跑的那一轮不受影响）。
        </p>
      </section>

      <section className="space-y-1.5">
        <div className="text-xs font-medium text-ink-dim">{label(K.maxConcurrentRuns)}</div>
        <div className="flex items-center gap-2">
          <input
            className="input w-28"
            inputMode="numeric"
            value={maxRuns}
            onChange={(e) => setMaxRuns(e.target.value)}
          />
          <span className="text-xs text-ink-faint">个（1~64）</span>
          <button
            className="btn btn-sm"
            aria-label="保存并发上限"
            disabled={!!busy || maxRuns === maxRunsValue}
            onClick={() => void onSave(K.maxConcurrentRuns, maxRuns, '并发上限')}
          >
            保存
          </button>
          <Overridden onReset={() => void onReset(K.maxConcurrentRuns, '并发上限')}
                      settings={settings} k={K.maxConcurrentRuns} />
        </div>
        <p className="text-[11px] text-ink-faint">
          每个回答都是一个全自主权限的 CLI 子进程；超过上限的新请求会被**直接拒绝**
          （不排队），「问 AI」页头部的「运行中」里能看到当前占用与并终止。
        </p>
      </section>

      <section className="space-y-1.5">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={skip}
            disabled={!!busy}
            onChange={(e) => void onSave(K.skipPermissions, String(e.target.checked), '全自主权限')}
          />
          <span>
            <span className="text-ink">{label(K.skipPermissions)}</span>
            <span className="block text-[11px] text-up">
              关掉后 codex 侧 MCP 工具会被直接拒绝（审批策略 never）；打开等于给 CLI
              无头全自主权限 —— 这条边界只适合本机单人使用，别把服务暴露到非本机地址。
            </span>
          </span>
        </label>
        <Overridden onReset={() => void onReset(K.skipPermissions, '全自主权限')}
                    settings={settings} k={K.skipPermissions} />
      </section>

      <section className="space-y-1.5">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={partial}
            disabled={!!busy}
            onChange={(e) => void onSave(K.partialMessages, String(e.target.checked), 'token 级流式')}
          />
          <span>
            <span className="text-ink">{label(K.partialMessages)}</span>
            <span className="block text-[11px] text-ink-faint">
              只对 claude 生效（codex CLI 本身没有 token 级增量）。老版本 claude CLI
              不认 --include-partial-messages 会直接报错退出，那种环境关掉即可。
            </span>
          </span>
        </label>
        <Overridden onReset={() => void onReset(K.partialMessages, 'token 级流式')}
                    settings={settings} k={K.partialMessages} />
      </section>

      <SourceHint settings={settings}
                  keys={[K.timeout, K.maxConcurrentRuns, K.skipPermissions, K.partialMessages]} />
    </div>
  );
}

/** 当前值已被覆盖时给一个「重置回配置默认」入口。 */
function Overridden({
  settings,
  k,
  onReset,
}: {
  settings?: SettingItem[];
  k: string;
  onReset: () => void;
}) {
  if (!isOverridden(settings, k)) return null;
  return (
    <button type="button" className="text-[11px] text-ink-dim hover:text-up" onClick={onReset}>
      已覆盖 · 重置为配置默认
    </button>
  );
}

/** 来源说明：让「代码默认 / 配置 / 我改过」一眼可见。 */
function SourceHint({ settings, keys }: { settings?: SettingItem[]; keys: string[] }) {
  const rows = (settings ?? []).filter((s) => keys.includes(s.key));
  if (rows.length === 0) return null;
  return (
    <ul className="border-t border-line pt-2 text-[11px] text-ink-faint">
      {rows.map((s) => (
        <li key={s.key} className="flex gap-2">
          <span className="w-40 shrink-0 truncate font-mono">{s.key}</span>
          <span className="min-w-0 flex-1 truncate">{String(s.value)}</span>
          <span className={s.source === 'runtime' ? 'text-gold' : ''}>
            {s.source === 'runtime' ? '已修改' : s.source === 'config' ? '配置' : '默认'}
          </span>
        </li>
      ))}
    </ul>
  );
}
