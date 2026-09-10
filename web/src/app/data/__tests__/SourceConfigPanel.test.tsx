import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SWRConfig } from 'swr';
import SourceConfigPanel from '../SourceConfigPanel';
import type { Provider, Setting } from '../types';

const SETTINGS: Setting[] = [
  {
    key: 'providers_order', value: ['tushare', 'akshare'], type: 'list',
    source: 'config', label: '源优先级', choices: null,
  },
  {
    key: 'crosscheck_peers', value: ['akshare'], type: 'list',
    source: 'config', label: '对拍 peers', choices: null,
  },
];

const PROVIDERS: Provider[] = [
  { name: 'tushare', enabled: true, capability: ['daily'], note: null },
  { name: 'akshare', enabled: true, capability: ['daily', 'etf_daily'], note: null },
  { name: 'sina', enabled: false, capability: ['daily'], note: null },
  { name: 'east', enabled: true, capability: ['fundamental'], note: null },
];

/** 按 URL 路由的 fetch mock（封装接口走封套 {code,data}） */
function routeFetch(recorder: { calls: { url: string; init?: RequestInit }[] }) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    recorder.calls.push({ url, init });
    let raw: unknown = null;
    try { raw = init?.body ? JSON.parse(String(init.body)) : null; } catch { /* ignore */ }
    const body = raw as { value?: string } | null;
    let data: unknown;
    if (url.endsWith('/settings')) data = SETTINGS;
    else if (url.endsWith('/settings/providers')) data = PROVIDERS;
    else if (url.includes('providers_order')) data = { key: 'providers_order', value: body?.value ?? '' };
    else if (url.includes('crosscheck_peers')) data = { key: 'crosscheck_peers', value: body?.value ?? '' };
    else return new Response('not found', { status: 404 });
    return new Response(JSON.stringify({ code: 0, data, message: 'ok' }, ), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  });
}

function renderPanel() {
  const recorder = { calls: [] as { url: string; init?: RequestInit }[] };
  vi.stubGlobal('fetch', routeFetch(recorder));
  // 每个用例独立 SWR cache，避免用例间串缓存
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <SourceConfigPanel />
    </SWRConfig>,
  );
  return recorder;
}

/** peer 标签是 button（select 的 option 也含同名文本，需用 role 区分） */
const peerTag = async (name: string) => await screen.findByRole('button', { name });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SourceConfigPanel', () => {
  it('peer 候选只含启用且声明 daily/etf_daily 的源', async () => {
    renderPanel();
    expect(await peerTag('tushare')).toBeInTheDocument();
    expect(await peerTag('akshare')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'sina' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'east' })).not.toBeInTheDocument();
  });

  it('初始未改动时保存按钮禁用（dirty 判定）', async () => {
    renderPanel();
    expect(await peerTag('akshare')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });

  it('切换 peer 后保存：PUT crosscheck_peers 与 providers_order，值正确', async () => {
    const user = userEvent.setup();
    const recorder = renderPanel();
    expect(await peerTag('tushare')).toBeInTheDocument();
    await user.click(await peerTag('tushare')); // 勾选 tushare 为 peer
    const save = screen.getByRole('button', { name: '保存' });
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(screen.getByText(/已保存/)).toBeInTheDocument());
    const bodyOf = (frag: string) => recorder.calls.find(
      (c) => c.init?.method === 'PUT' && c.url.includes(frag),
    );
    expect(JSON.parse(String(bodyOf('crosscheck_peers')?.init?.body)).value)
      .toBe('akshare,tushare');
    // 主源保持 tushare 不变，providers_order 照常保存
    expect(JSON.parse(String(bodyOf('providers_order')?.init?.body)).value)
      .toBe('tushare,akshare');
  });

  it('主源替换：选新主源保存后 providers_order 首位换成新源', async () => {
    const user = userEvent.setup();
    const recorder = renderPanel();
    expect(await peerTag('akshare')).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox'), 'akshare');
    await user.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(screen.getByText(/已保存（主源/)).toBeInTheDocument());
    const put = recorder.calls.find(
      (c) => c.init?.method === 'PUT' && c.url.includes('providers_order'),
    );
    // akshare 原为第 2 位：升首位后其余跟随（组件逻辑：原首位被替换）
    expect(JSON.parse(String(put?.init?.body)).value).toBe('akshare');
  });
});
