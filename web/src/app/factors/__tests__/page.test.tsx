import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import FactorsPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

vi.mock('@/lib/streaming', () => ({
  useJobStream: () => ({
    error: null,
    result: null,
    done: false,
    status: null,
    progress: null,
  }),
}));

const factors = [
  {
    name: 'pct_change_20', expression: 'pct_change(close,20)', description: '20日动量',
    created_at: '2026-09-01', source: 'manual', ic_neutral: 0.03, category: '动量',
  },
  {
    name: 'rolling_std_20', expression: 'rolling_std(close,20)', description: '20日波动',
    created_at: '2026-09-02', source: 'qlib', ic_neutral: -0.01, category: '波动',
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('FactorsPage', () => {
  it('正常数据：渲染因子页与内置因子计数', async () => {
    stubPageFetch({
      '/factors/builtin': [
        { name: 'alpha101', expression: 'x', description: 'd', created_at: '2026-01-01' },
        { name: 'alpha102', expression: 'x', description: 'd', created_at: '2026-01-02' },
      ],
      '/factors/universes': [{ key: 'all', index_code: null, label: '全市场' }],
      '/factors': factors,
    });
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
    expect(screen.getByText(/内置 Qlib Alpha158 2 个/)).toBeInTheDocument();
    // 快速评价表单壳（tab 与标题同名，断言存在即可）
    expect(screen.getAllByText('快速评价').length).toBeGreaterThan(0);
  });

  it('fetch 失败：壳仍在，不崩溃', async () => {
    stubPageFetch({});
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子')).toBeInTheDocument());
    expect(screen.getAllByText('快速评价').length).toBeGreaterThan(0);
  });

  it('切换到因子库 tab：渲染已注册因子', async () => {
    stubPageFetch({
      '/factors/builtin': [],
      '/factors/universes': [],
      '/factors': factors,
    });
    renderPage(<FactorsPage />);

    await waitFor(() => expect(screen.getByText('因子库')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '因子库' }));
    await waitFor(() => expect(screen.getByText('pct_change_20')).toBeInTheDocument());
  });
});
