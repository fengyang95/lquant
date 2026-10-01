import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SWRConfig } from 'swr';

import FactorLibrary from '../FactorLibrary';
import type { FactorRow } from '../FactorLibrary';

const { mockGet, mockPost, mockPut, mockDel } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPut: vi.fn(),
  mockDel: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  get: mockGet,
  post: mockPost,
  put: mockPut,
  del: mockDel,
}));

function renderLib(factors: FactorRow[]) {
  const mutate = vi.fn();
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <FactorLibrary factors={factors} mutate={mutate} />
    </SWRConfig>,
  );
  return mutate;
}

const ROWS: FactorRow[] = [
  { name: 'mom20', expression: 'Rank(Ts_Mean($close,5)/$close-1)', description: '',
    created_at: '2026-09-01', source: 'manual', ic_neutral: 0.03, category: '自定义' },
  { name: 'ALPHA158_KBAR', expression: 'x', description: '',
    created_at: '2026-09-02', source: 'qlib', ic_neutral: null, category: 'alpha158·kbar' },
];

describe('FactorLibrary 编辑 / 删除', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue([]);
    vi.stubGlobal('confirm', vi.fn(() => true));
  });

  it('manual 因子可编辑：弹层保存走 PUT /factors/{name}', async () => {
    const mutate = renderLib(ROWS);
    fireEvent.click(await screen.findByText('编辑'));
    const inputs = screen.getAllByDisplayValue('Rank(Ts_Mean($close,5)/$close-1)');
    fireEvent.change(inputs[0], { target: { value: 'Rank($close/Ts_Mean($close,10)-1)' } });
    mockPut.mockResolvedValue({ updated: 'mom20', rows: 1 });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalledWith('/factors/mom20', {
        expression: 'Rank($close/Ts_Mean($close,10)-1)',
        description: '',
        category: null,
      });
    });
    expect(mutate).toHaveBeenCalled();
  });

  it('种子因子（qlib）不显示编辑/删除', async () => {
    renderLib(ROWS);
    // manual 行有编辑，qlib 行没有第二个编辑按钮
    await screen.findByText('mom20');
    expect(screen.getAllByRole('button', { name: '编辑' }).length).toBe(1);
    expect(screen.getAllByRole('button', { name: '删除' }).length).toBe(1);
  });

  it('删除需确认，确认后走 DELETE 并 mutate', async () => {
    const mutate = renderLib(ROWS);
    mockDel.mockResolvedValue({ deleted: 'mom20' });
    fireEvent.click(await screen.findByText('删除'));
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith('/factors/mom20'));
    expect(mutate).toHaveBeenCalled();
  });

  it('取消确认时不发删除请求', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    renderLib(ROWS);
    fireEvent.click(await screen.findByText('删除'));
    expect(mockDel).not.toHaveBeenCalled();
  });

  it('默认按 IC(中性化) 保持服务端顺序，可切换为按名称', async () => {
    // 服务端已按 ic_neutral DESC 返回：zzz 在前（IC 高），名字序相反
    const ordered: FactorRow[] = [
      { name: 'zzz_mom', expression: 'x', description: '', created_at: '2026-09-01',
        source: 'manual', ic_neutral: 0.05, category: '动量' },
      { name: 'aaa_vol', expression: 'y', description: '', created_at: '2026-09-02',
        source: 'manual', ic_neutral: -0.02, category: '波动' },
    ];
    renderLib(ordered);
    const rowNames = () => screen.getAllByRole('link')
      .map((a) => a.textContent ?? '')
      .filter((t) => t === 'zzz_mom' || t === 'aaa_vol');

    await screen.findByText('zzz_mom');
    expect(rowNames()).toEqual(['zzz_mom', 'aaa_vol']);

    fireEvent.click(screen.getByRole('button', { name: '按名称' }));
    await waitFor(() => expect(rowNames()).toEqual(['aaa_vol', 'zzz_mom']));
  });

  it('来源 Tab 优先取 /factors/sources', async () => {
    mockGet.mockImplementation((path: string) => Promise.resolve(
      path.includes('/factors/sources')
        ? [{ name: 'qlib', label: 'Qlib' }, { name: 'agent', label: 'Agent' }]
        : [],
    ));
    renderLib(ROWS);
    await waitFor(() => expect(screen.getByRole('button', { name: 'agent' })).toBeInTheDocument());
  });
});
