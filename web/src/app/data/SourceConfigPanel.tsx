'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Msg } from '@/components/States';
import { getData, putData } from '@/lib/api';
import type { Provider, Setting } from './types';

/** 可作为对拍 peer 的源：capability 含 daily 或 etf_daily 且 enabled */
function peerCandidates(providers: Provider[] | undefined): Provider[] {
  return (providers ?? []).filter(
    (p) => p.enabled && (p.capability.includes('daily') || p.capability.includes('etf_daily')),
  );
}

/** 数据源配置卡：主源下拉（providers_order 首位）+ 对拍 peers 多选 */
export default function SourceConfigPanel() {
  const { data: settings, mutate: mutateSettings } = useSWR<Setting[]>('/settings', getData);
  const { data: providers } = useSWR<Provider[]>('/settings/providers', getData);
  const [primary, setPrimary] = useState('');
  const [peers, setPeers] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  // 读现有值初始化草稿（providers_order 首位 = 主源；其余位次保留）
  useEffect(() => {
    if (dirty || !settings) return;
    const order = settings.find((s) => s.key === 'providers_order');
    const peersSetting = settings.find((s) => s.key === 'crosscheck_peers');
    const orderList = Array.isArray(order?.value) ? (order?.value as string[]) : [];
    setPrimary(orderList[0] ?? '');
    setPeers(Array.isArray(peersSetting?.value) ? (peersSetting?.value as string[]) : []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, dirty]);

  const candidates = peerCandidates(providers);

  function togglePeer(name: string) {
    setPeers((prev) =>
      prev.includes(name) ? prev.filter((p) => p !== name) : [...prev, name],
    );
    setDirty(true);
  }

  async function save() {
    setBusy(true);
    setMsg('');
    try {
      if (primary) {
        const orderSetting = settings?.find((s) => s.key === 'providers_order');
        const rest = Array.isArray(orderSetting?.value)
          ? (orderSetting!.value as string[]).slice(1)
          : [];
        const newOrder = [primary, ...rest.filter((n) => n !== primary)];
        await putData(`/settings/providers_order`, {
          key: 'providers_order',
          value: newOrder.join(','),
        });
      }
      if (peers.length) {
        await putData('/settings/crosscheck_peers', {
          key: 'crosscheck_peers',
          value: peers.join(','),
        });
      }
      setMsg('✓ 已保存，下一次拉取即生效');
      setDirty(false);
      void mutateSettings();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="数据源配置" meta="主源 = 拉取优先级首位">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-1 text-xs text-ink-faint">主源（日线拉取优先使用）</div>
          <select
            className="input w-full"
            value={primary}
            onChange={(e) => {
              setPrimary(e.target.value);
              setDirty(true);
            }}
          >
            <option value="">— 选择主源 —</option>
            {(providers ?? []).filter((p) => p.enabled).map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-ink-faint">
            写入 providers_order 首位，其余源按原顺序跟随；失败时依次降级。
          </p>
        </div>
        <div>
          <div className="mb-1 text-xs text-ink-faint">对拍 peer 源（跨源印证用，可多选）</div>
          {!candidates.length ? (
            <p className="text-sm text-ink-faint">无可用 peer 源（需 capability 含 daily/etf_daily 且启用）</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {candidates.map((p) => (
                <button
                  key={p.name}
                  onClick={() => togglePeer(p.name)}
                  className={`tag ${peers.includes(p.name) ? 'tag-on' : ''}`}
                >
                  {p.name}
                </button>
              ))}
            </div>
          )}
          <p className="mt-1 text-xs text-ink-faint">写入 crosscheck_peers；只接受声明 daily/etf_daily 的已启用源。</p>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button className="btn btn-primary btn-sm" onClick={save} disabled={busy || (!dirty && !msg.startsWith('✓'))}>
          {busy ? '保存中…' : '保存'}
        </button>
        <Msg text={msg} />
      </div>
    </Panel>
  );
}
