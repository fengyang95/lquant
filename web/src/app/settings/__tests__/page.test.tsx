import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import SettingsPage from '../page';
import { okEnvelope, renderPage, stubPageFetch } from '@/test/page-utils';

const items = [
  { key: 'rebalance.frequency', value: 'monthly', type: 'enum', source: 'default', label: '调仓频率', choices: ['monthly', 'weekly'] },
  { key: 'risk.limit', value: true, type: 'bool', source: 'runtime', label: '风控开关', choices: null },
  { key: 'pool.etf', value: ['510300.SH', '159915.SZ'], type: 'list', source: 'config', label: 'ETF 池', choices: null },
];

const providers = [
  { name: 'tushare', enabled: true, capability: ['daily', 'basic'], note: null },
  { name: 'legacy', enabled: false, capability: ['daily'], note: null },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SettingsPage', () => {
  it('正常数据：渲染配置项与数据源优先级', async () => {
    stubPageFetch({
      '/settings/providers': providers,
      '/settings': items, // 注意顺序：'/settings' 也命中 '/settings/providers' 前缀，后查表先命中
    });
    renderPage(<SettingsPage />);

    await waitFor(() => expect(screen.getByText('rebalance.frequency')).toBeInTheDocument());
    expect(screen.getByText('设置')).toBeInTheDocument();
    expect(screen.getByText('调仓频率')).toBeInTheDocument();
    expect(screen.getByText('tushare')).toBeInTheDocument();
    expect(screen.getByText('停用')).toBeInTheDocument();
    // enum 下拉渲染选项
    expect(screen.getByDisplayValue('monthly')).toBeInTheDocument();
  });

  it('fetch 失败：壳仍在（h1/面板），不崩溃', async () => {
    stubPageFetch({});
    renderPage(<SettingsPage />);

    await waitFor(() => expect(screen.getByText('设置')).toBeInTheDocument());
    expect(screen.getByText(/运行时配置（0）/)).toBeInTheDocument();
    expect(screen.getByText('暂无数据源配置')).toBeInTheDocument();
  });
});
