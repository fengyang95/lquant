import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import SectorsPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

const rows = [
  {
    trade_date: '2026-09-16', sector_name: '半导体', change_pct: 3.2,
    main_net_inflow: 8.6e8, up_count: 42, down_count: 5,
  },
  {
    trade_date: '2026-09-16', sector_name: '白酒', change_pct: -1.4,
    main_net_inflow: -2.3e8, up_count: 8, down_count: 39,
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SectorsPage', () => {
  it('正常数据：渲染板块表', async () => {
    stubPageFetch({ '/market/sectors?kind=industry': rows });
    renderPage(<SectorsPage />);

    await waitFor(() => expect(screen.getByText('半导体')).toBeInTheDocument());
    expect(screen.getByText('白酒')).toBeInTheDocument();
    expect(screen.getByText('主力净流入（亿）')).toBeInTheDocument();
    expect(screen.getByText('+3.20%')).toBeInTheDocument();
  });

  it('fetch 失败：ErrorNote 错误态', async () => {
    stubPageFetch({ '/market/sectors?kind=industry': { detail: 'boom' } }, { '/market/sectors?kind=industry': 500 });
    renderPage(<SectorsPage />);

    await waitFor(() => expect(screen.getByText(/加载失败/)).toBeInTheDocument());
  });

  it('空数据：空态提示触发采集', async () => {
    stubPageFetch({ '/market/sectors?kind=industry': [] });
    renderPage(<SectorsPage />);

    await waitFor(() => expect(screen.getByText(/暂无板块数据/)).toBeInTheDocument());
  });
});
