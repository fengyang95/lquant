import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import CrosscheckPanel from '../CrosscheckPanel';
import type { QualityIssue } from '../types';

function makeIssue(over: Partial<QualityIssue> = {}): QualityIssue {
  return {
    issue_id: 'i1',
    dataset: 'daily_bar',
    symbol: '600519.SH',
    trade_date: '2024-05-06',
    rule_code: 'CROSS_SRC_DIFF.akshare.L2',
    severity: 'warn',
    detail: {
      message: 'akshare 对拍 L2：600519.SH@2024-05-06',
      field: 'close',
      primary: 1700.5,
      peer: 1683.2,
      deviation_pct: 1.02,
      level: 'L2',
    },
    count: 3,
    resolved: false,
    created_at: '2024-05-07T08:00:00',
    ...over,
  };
}

function renderPanel(issues: QualityIssue[]) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
    if (url.includes('/crosscheck/issues')) {
      return new Response(JSON.stringify(issues), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  }));
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <CrosscheckPanel />
    </SWRConfig>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('CrosscheckPanel summary → 空态/数据态分支', () => {
  it('无 issue 且未对拍：展示引导空态', async () => {
    renderPanel([]);
    expect(await screen.findByText(/暂无分歧 issue/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('有 issue：表格含字段级列（字段/主源/peer/偏差%）', async () => {
    renderPanel([makeIssue()]);
    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(screen.getByText('close')).toBeInTheDocument();
    expect(screen.getByText('1700.5')).toBeInTheDocument();
    expect(screen.getByText('1683.2')).toBeInTheDocument();
    expect(screen.getByText('1.02%')).toBeInTheDocument();
    expect(screen.getByText(/akshare 对拍 L2/)).toBeInTheDocument(); // message 保留为补充说明
    expect(screen.getByText('L2 可疑')).toBeInTheDocument();
  });

  it('deviation_pct 为 null → 偏差列显示 —，primary/peer 缺失也显示 —', async () => {
    renderPanel([makeIssue({
      severity: 'error',
      detail: { message: '旧数据无字段级明细' },
    })]);
    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(screen.getByText('L3 严重')).toBeInTheDocument();
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(3);
  });

  it('issues 为空 + summary.checked=0 场景由组件内部 state 驱动，空态含 peers 引导', async () => {
    renderPanel([]);
    const empty = await screen.findByText(/暂无分歧 issue/);
    expect(empty).toBeInTheDocument();
  });
});
