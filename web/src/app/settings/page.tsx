'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { getData, putData } from '@/lib/api';

type Setting = {
  key: string;
  value: unknown;
  type: 'str' | 'bool' | 'enum' | 'list';
  source: 'default' | 'config' | 'runtime';
  label: string;
  choices: string[] | null;
};

type ProviderRow = {
  name: string;
  enabled: boolean;
  capability: string[];
  note: string | null;
};

const SOURCE_LABEL: Record<string, string> = {
  default: '默认', config: '配置', runtime: '已修改',
};

export default function SettingsPage() {
  const { data: items, mutate } = useSWR<Setting[]>('/settings', getData);
  const { data: providers } = useSWR<ProviderRow[]>('/settings/providers', getData);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');

  async function save(key: string, value: string) {
    setBusy(key);
    setMsg('');
    try {
      const r = await putData<{ key: string; value: unknown }>(`/settings/${key}`, { key, value });
      setMsg(`✓ ${key} = ${String(r.value)}`);
      // 落库成功后用返回值同步（清除）该 key 的草稿，避免输入框显示与实际不一致
      setDraft((d) => {
        if (!(key in d)) return d;
        const { [key]: _removed, ...rest } = d;
        return rest;
      });
      await mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">设置</h1>
        <span className="text-xs text-neutral-400">覆盖层：代码默认 &lt; config/yaml &lt; 运行时（此处写入 app_setting）</span>
      </div>

      {msg && <div className="rounded-md border bg-white px-4 py-2 text-sm">{msg}</div>}

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">运行时配置（{items?.length ?? 0}）</div>
        {!items?.length ? (
          <div className="py-8 text-center text-sm text-neutral-400">加载中…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">配置项</th>
                <th className="text-left font-normal">当前值</th>
                <th className="text-left font-normal">来源</th>
                <th className="text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const current = draft[it.key] ?? String(it.value);
                return (
                  <tr key={it.key} className="border-b border-neutral-50">
                    <td className="py-2 align-top">
                      <div className="font-mono text-xs">{it.key}</div>
                      <div className="text-xs text-neutral-400">{it.label}</div>
                    </td>
                    <td className="py-2 align-top">
                      {it.type === 'enum' ? (
                        <select value={current}
                                onChange={(e) => setDraft((d) => ({ ...d, [it.key]: e.target.value }))}
                                className="rounded-md border px-2 py-1 text-sm text-neutral-900">
                          {(it.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
                        </select>
                      ) : it.type === 'bool' ? (
                        <select value={current}
                                onChange={(e) => setDraft((d) => ({ ...d, [it.key]: e.target.value }))}
                                className="rounded-md border px-2 py-1 text-sm text-neutral-900">
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      ) : (
                        <input value={current}
                               onChange={(e) => setDraft((d) => ({ ...d, [it.key]: e.target.value }))}
                               className="w-64 rounded-md border px-2 py-1 font-mono text-sm text-neutral-900" />
                      )}
                      <div className="mt-1 text-xs text-neutral-400">默认：{String(it.type === 'list' ? (it.value as string[]).join(',') : it.value)}</div>
                    </td>
                    <td className="py-2 align-top">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${
                        it.source === 'runtime' ? 'bg-amber-50 text-amber-700' : 'bg-neutral-100 text-neutral-400'}`}>
                        {SOURCE_LABEL[it.source]}
                      </span>
                    </td>
                    <td className="py-2 text-right align-top">
                      <button onClick={() => save(it.key, current)} disabled={busy === it.key}
                              className="rounded border px-2 py-0.5 text-xs hover:bg-neutral-100 disabled:opacity-40">
                        {busy === it.key ? '…' : '保存'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium">数据源优先级（config/providers.yaml，自上而下）</div>
        {!providers?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">暂无数据源配置</div>
        ) : (
          <ol className="space-y-1.5 text-sm">
            {providers.filter((p) => p.enabled).concat(providers.filter((p) => !p.enabled)).map((p, i) => (
              <li key={p.name} className="flex items-center gap-3">
                <span className="w-5 text-right font-mono text-xs text-neutral-400">{i + 1}</span>
                <span className="font-medium">{p.name}</span>
                <span className="text-xs text-neutral-500">{p.capability.slice(0, 6).join(' · ') || '—'}</span>
                {!p.enabled && <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-400">停用</span>}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}