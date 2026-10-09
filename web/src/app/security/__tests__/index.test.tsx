/** 个股分析入口页：默认全量清单 + 行业/代码/名称/类型/板块筛选 + 分页。 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  useParams: () => ({}),
}));

type Item = {
  symbol: string; name: string; sec_type: string; board: string;
  list_date: string; is_st: boolean;
  industry: string | null; industry_code: string | null;
};

const UNIVERSE = {
  asof: '2026-06-30',
  std: 'SW',
  total: 3,
  limit: 50,
  offset: 0,
  items: [
    { symbol: '000001.SZ', name: '平安银行', sec_type: 'stock', board: 'main',
      list_date: '1991-04-03', is_st: false,
      industry: '银行', industry_code: '801780.SI' },
    { symbol: '600519.SH', name: '贵州茅台', sec_type: 'stock', board: 'main',
      list_date: '2001-08-27', is_st: false,
      industry: '食品饮料', industry_code: '801120.SI' },
    { symbol: '510300.SH', name: '沪深300ETF', sec_type: 'etf', board: 'unknown',
      list_date: '2012-05-28', is_st: false, industry: null, industry_code: null },
  ] as Item[],
  industries: [
    { code: '801780.SI', name: '银行', n_members: 2 },
    { code: '801120.SI', name: '食品饮料', n_members: 1 },
  ],
  notes: [] as string[],
};

const WATCH_ROW = {
  symbol: '600036.SH', name: '招商银行', close: 40,
  trade_date: '2026-09-30', change_pct: 1.2,
};

const ANGLE_META = {
  schema_version: '1.0',
  angles: [
    { id: 'technical', label: '技术面', weight: 0.24, desc: '趋势 / 动量' },
    { id: 'news', label: '消息面', weight: 0, desc: '近期新闻热度（仅信息）' },
  ],
};

/** 可变夹具：让「空清单 / 报错 / 自选为空」这类分支不必重载模块即可覆盖 */
let universeData: typeof UNIVERSE = UNIVERSE;
let universeError: unknown = undefined;
let watchData: typeof WATCH_ROW[] = [WATCH_ROW];
const requestedKeys: string[] = [];

vi.mock('swr', () => ({
  default: (key: string | null) => {
    if (key) requestedKeys.push(key);
    if (!key) return { data: undefined, isLoading: false, error: undefined };
    if (key.startsWith('/data/securities/universe')) {
      return { data: universeData, isLoading: false, error: universeError };
    }
    if (key === '/watchlist') return { data: watchData, isLoading: false, error: undefined };
    if (key === '/security/angles') return { data: ANGLE_META, isLoading: false, error: undefined };
    return { data: undefined, isLoading: false, error: undefined };
  },
}));

vi.mock('@/lib/api', () => ({ fetcher: vi.fn() }));

import SecurityIndexPage from '../page';

/** 取出所有全量清单请求的查询参数 */
function universeQueries(): URLSearchParams[] {
  return requestedKeys
    .filter((k) => k.startsWith('/data/securities/universe'))
    .map((k) => new URL(k, 'http://localhost').searchParams);
}

describe('个股分析入口页', () => {
  beforeEach(() => {
    push.mockClear();
    requestedKeys.length = 0;
    universeData = UNIVERSE;
    universeError = undefined;
    watchData = [WATCH_ROW];
  });

  it('默认加载并展示全量标的清单（无筛选）', async () => {
    render(<SecurityIndexPage />);
    expect(await screen.findByText('平安银行')).toBeInTheDocument();
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('沪深300ETF')).toBeInTheDocument();
    // 行业归属与总数都透出
    expect(screen.getByText('食品饮料')).toBeInTheDocument();
    expect(screen.getByText(/第 1 \/ 1 页 · 共 3 只/)).toBeInTheDocument();

    const first = universeQueries()[0];
    expect(first.get('limit')).toBe('50');
    expect(first.get('offset')).toBe('0');
    expect(first.get('q')).toBeNull();
    expect(first.get('industry')).toBeNull();
  });

  it('行业下拉来自后端行业清单，选中后按行业代码筛选', async () => {
    render(<SecurityIndexPage />);
    const select = await screen.findByLabelText('行业');
    expect(await screen.findByRole('option', { name: '银行（2）' })).toBeInTheDocument();

    await userEvent.selectOptions(select, '801780.SI');
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('industry') === '801780.SI')).toBe(true);
    });
  });

  it('代码/名称关键词防抖后进入筛选参数', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('代码或名称'), '茅台');
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('q') === '茅台')).toBe(true);
    }, { timeout: 2000 });
  });

  it('类型与板块筛选进入请求参数', async () => {
    render(<SecurityIndexPage />);
    await userEvent.selectOptions(await screen.findByLabelText('类型'), 'etf');
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('sec_type') === 'etf')).toBe(true);
    });
    await userEvent.selectOptions(screen.getByLabelText('板块'), 'main');
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('board') === 'main')).toBe(true);
    });
  });

  it('「重置」清掉全部筛选条件', async () => {
    render(<SecurityIndexPage />);
    await userEvent.selectOptions(await screen.findByLabelText('行业'), '801780.SI');
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('industry') === '801780.SI')).toBe(true);
    });
    await userEvent.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => {
      const last = universeQueries().at(-1)!;
      expect(last.get('industry')).toBeNull();
      expect(last.get('q')).toBeNull();
      expect(last.get('offset')).toBe('0');
    });
  });

  it('分页：下一页 offset=50，首页「上一页」禁用', async () => {
    universeData = { ...UNIVERSE, total: 120 };
    render(<SecurityIndexPage />);
    const prev = await screen.findByRole('button', { name: '上一页' });
    expect(prev).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => {
      expect(universeQueries().some((p) => p.get('offset') === '50')).toBe(true);
    });
    expect(screen.getByText(/第 2 \/ 3 页 · 共 120 只/)).toBeInTheDocument();
  });

  it('每行的「分析」链接指向该标的详情页', async () => {
    render(<SecurityIndexPage />);
    const row = (await screen.findByText('贵州茅台')).closest('tr') as HTMLElement;
    expect(row.querySelector('a')).toHaveAttribute('href', '/security/600519.SH');
  });

  it('输入代码后点「分析该代码」跳到分析页', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('代码或名称'), '600519');
    const btn = screen.getByRole('button', { name: '分析该代码' });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    expect(push).toHaveBeenCalledWith('/security/600519');
  });

  it('名称关键字只筛选清单，不触发跳转', async () => {
    render(<SecurityIndexPage />);
    await userEvent.type(screen.getByLabelText('代码或名称'), '茅台');
    expect(screen.getByRole('button', { name: '分析该代码' })).toBeDisabled();
    expect(screen.getByText(/代码形如 6 位数字/)).toBeInTheDocument();
  });

  it('清单为空时给出可执行的下一步', async () => {
    universeData = { ...UNIVERSE, total: 0, items: [] };
    render(<SecurityIndexPage />);
    expect(await screen.findByText(/标的库为空/)).toBeInTheDocument();
  });

  it('后端 notes（如行业分类缺失）如实透出', async () => {
    universeData = { ...UNIVERSE, notes: ['行业分类数据为空（industry_classify）'] };
    render(<SecurityIndexPage />);
    expect(await screen.findByText(/行业分类数据为空/)).toBeInTheDocument();
  });

  it('清单加载失败时给出错误提示', async () => {
    universeError = new Error('boom');
    render(<SecurityIndexPage />);
    expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
  });

  it('展示各分析角度与权重', async () => {
    render(<SecurityIndexPage />);
    expect(await screen.findByText('技术面')).toBeInTheDocument();
    expect(screen.getByText('权重 24%')).toBeInTheDocument();
    // 权重为 0 的角度明确标注不参与评分
    expect(screen.getByText('不参与评分')).toBeInTheDocument();
  });

  it('自选列表提供直达入口，为空时给出下一步', async () => {
    render(<SecurityIndexPage />);
    expect(await screen.findByText('招商银行')).toBeInTheDocument();

    watchData = [];
    render(<SecurityIndexPage />);
    expect(await screen.findByText(/自选为空/)).toBeInTheDocument();
  });
});
