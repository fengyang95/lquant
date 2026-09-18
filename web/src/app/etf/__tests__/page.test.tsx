import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import EtfPage from '../page';
import { okEnvelope, renderPage, stubPageFetch } from '@/test/page-utils';

const meta = [
  {
    symbol: '510300.SH', name: '沪深300ETF', track_index: '沪深300', fund_type: '指数型',
    is_cross_border: false, sellable_after_days: 0, management_fee: 0.5, custody_fee: 0.1,
    fund_size: 3.2e9, as_of: '2026-09-16',
  },
  {
    symbol: '513100.SH', name: '纳指ETF', track_index: '纳斯达克100', fund_type: 'QDII',
    is_cross_border: true, sellable_after_days: 2, management_fee: 0.8, custody_fee: 0.25,
    fund_size: 1.1e9, as_of: '2026-09-16',
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('EtfPage', () => {
  it('正常数据：渲染元数据表与跨境占比', async () => {
    stubPageFetch({
      '/etf/meta': meta,
      '/etf/correlation': {
        symbols: ['510300.SH', '513100.SH'],
        pairs: [{ a: '510300.SH', b: '513100.SH', corr: 0.42 }],
        note: null,
      },
    });
    renderPage(<EtfPage />);

    await waitFor(() => expect(screen.getAllByText('510300.SH').length).toBeGreaterThan(0));
    expect(screen.getByText('ETF 专区')).toBeInTheDocument();
    expect(screen.getByText('纳指ETF')).toBeInTheDocument();
    expect(screen.getAllByText('跨境').length).toBeGreaterThan(0);
    // 概览：境内/跨境
    expect(screen.getByText('跨境占比')).toBeInTheDocument();
    expect(screen.getByText('0.420')).toBeInTheDocument();
  });

  it('fetch 失败：降级为空态，不崩溃', async () => {
    stubPageFetch({});
    renderPage(<EtfPage />);

    await waitFor(() => expect(screen.getByText(/暂无 ETF 元数据/)).toBeInTheDocument());
    expect(screen.getByText('ETF 专区')).toBeInTheDocument();
  });
});
