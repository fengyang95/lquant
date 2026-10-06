/** 个股分析入口页：代码输入 → 跳转到该标的分析；自选与角度说明。 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  useParams: () => ({}),
}));

const WATCH_ROW = {
  symbol: '600519.SH', name: '贵州茅台', close: 1500,
  trade_date: '2026-09-30', change_pct: 1.2,
};

/** 可变夹具：让「自选为空」这类分支不必重载模块即可覆盖 */
let watchData: typeof WATCH_ROW[] = [WATCH_ROW];

const ANGLE_META = {
  schema_version: '1.0',
  angles: [
    { id: 'technical', label: '技术面', weight: 0.24, desc: '趋势 / 动量' },
    { id: 'news', label: '消息面', weight: 0, desc: '近期新闻热度（仅信息）' },
  ],
};

const SECURITIES = [{ symbol: '000001.SZ', name: '平安银行', sec_type: 'stock' }];

vi.mock('swr', () => ({
  default: (key: string | null) => {
    if (!key) return { data: undefined, isLoading: false, error: undefined };
    if (key === '/watchlist') return { data: watchData, isLoading: false, error: undefined };
    if (key === '/security/angles') return { data: ANGLE_META, isLoading: false, error: undefined };
    if (key.startsWith('/data/securities')) {
      return { data: SECURITIES, isLoading: false, error: undefined };
    }
    return { data: undefined, isLoading: false, error: undefined };
  },
}));

vi.mock('@/lib/api', () => ({ fetcher: vi.fn() }));

import SecurityIndexPage from '../page';

describe('个股分析入口页', () => {
  beforeEach(() => {
    push.mockClear();
    watchData = [WATCH_ROW];
  });

  it('输入裸代码后点「开始分析」跳到分析页', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('股票代码'), '600519');
    await userEvent.click(screen.getByRole('button', { name: '开始分析' }));
    expect(push).toHaveBeenCalledWith('/security/600519');
  });

  it('表单提交（回车）同样触发跳转', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('股票代码'), '000001.SZ{Enter}');
    expect(push).toHaveBeenCalledWith('/security/000001.SZ');
  });

  it('输入为空时提交按钮禁用', () => {
    render(<SecurityIndexPage />);
    expect(screen.getByRole('button', { name: '开始分析' })).toBeDisabled();
  });

  it('名称搜索结果可点进分析页（带交易所后缀）', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('股票代码'), '平安');
    const hit = await screen.findByText('平安银行');
    await userEvent.click(hit.closest('button') as HTMLElement);
    expect(push).toHaveBeenCalledWith('/security/000001.SZ');
  });

  it('自选列表提供直达入口', async () => {
    render(<SecurityIndexPage />);
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '分析' }))
      .toHaveAttribute('href', '/security/600519.SH');
  });

  it('展示各分析角度与权重', async () => {
    render(<SecurityIndexPage />);
    expect(await screen.findByText('技术面')).toBeInTheDocument();
    expect(screen.getByText('权重 24%')).toBeInTheDocument();
    // 权重为 0 的角度明确标注不参与评分
    expect(screen.getByText('不参与评分')).toBeInTheDocument();
  });

  it('自选为空时给出可执行下一步', async () => {
    watchData = [];
    render(<SecurityIndexPage />);
    expect(await screen.findByText(/自选为空/)).toBeInTheDocument();
  });
});
