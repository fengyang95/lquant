import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SWRConfig } from 'swr';
import DataDictionaryModal from '../DataDictionaryModal';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderModal(json: unknown, status = 200) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
    new Response(JSON.stringify(json), {
      status, headers: { 'Content-Type': 'application/json' },
    }),
  ));
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <DataDictionaryModal onClose={() => {}} />
    </SWRConfig>,
  );
}

describe('DataDictionaryModal 数据字典', () => {
  const dict = {
    tables: [
      {
        table: 'daily',
        description: '日线行情',
        fields: [
          { name: 'symbol', type: 'str', description: '标的代码' },
          { name: 'close', type: 'f64', description: '收盘价' },
        ],
      },
      {
        table: 'adj_factor',
        description: '复权因子',
        fields: [{ name: 'factor', type: 'f64', description: '因子值' }],
      },
    ],
  };

  it('按表分组渲染字段表', async () => {
    renderModal(dict);
    expect(await screen.findByText('daily')).toBeInTheDocument();
    expect(screen.getByText('日线行情')).toBeInTheDocument();
    expect(screen.getByText('symbol')).toBeInTheDocument();
    expect(screen.getByText('收盘价')).toBeInTheDocument();
    expect(screen.getByText('adj_factor')).toBeInTheDocument();
  });

  it('空字典：空态文案', async () => {
    renderModal({ tables: [] });
    expect(await screen.findByText('字典为空')).toBeInTheDocument();
  });

  it('加载失败：错误态', async () => {
    renderModal({ detail: 'boom' }, 500);
    expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
  });
});
