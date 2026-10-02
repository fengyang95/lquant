// 个股详情页 —— 指标选择器驱动 /data/indicators 的 names 参数；基本面卡片挂载
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const REGISTRY = {
  default: ['ma', 'macd', 'rsi', 'boll'],
  indicators: [
    { name: 'ma', label: '均线族', category: 'trend', pane: 'price', min_window: 60,
      inputs: ['close'], outputs: ['ma5', 'ma20'] },
    { name: 'macd', label: 'MACD', category: 'trend', pane: 'sub', min_window: 60,
      inputs: ['close'], outputs: ['macd_dif', 'macd_hist'] },
    { name: 'rsi', label: 'RSI', category: 'oscillator', pane: 'sub', min_window: 40,
      inputs: ['close'], outputs: ['rsi14'] },
    { name: 'boll', label: '布林带', category: 'channel', pane: 'price', min_window: 40,
      inputs: ['close'], outputs: ['boll_upper', 'boll_mid'] },
    { name: 'kdj', label: 'KDJ', category: 'oscillator', pane: 'sub', min_window: 30,
      inputs: ['high', 'low', 'close'], outputs: ['kdj_k', 'kdj_d'] },
    { name: 'tiandao', label: '天道通道（金牛/金钻）', category: 'channel',
      pane: 'price', min_window: 60, inputs: ['high', 'low'],
      outputs: ['td_jinniu', 'td_gold_buy'] },
  ],
};

const ROWS = [
  { trade_date: '2026-06-29', open: 1, high: 2, low: 0.5, close: 1.5, volume: 100,
    ma5: 1.4, ma20: 1.3, macd_dif: 0.01, macd_hist: 0.02, rsi14: 55,
    boll_upper: 1.8, boll_mid: 1.5, kdj_k: 60, kdj_d: 55, kdj_j: 70,
    td_jinniu: 1.7, td_gold_buy: true },
  { trade_date: '2026-06-30', open: 1, high: 2, low: 0.5, close: 1.6, volume: 110,
    ma5: 1.45, ma20: 1.32, macd_dif: 0.02, macd_hist: 0.03, rsi14: 58,
    boll_upper: 1.9, boll_mid: 1.55, kdj_k: 65, kdj_d: 58, kdj_j: 79,
    td_jinniu: 1.75, td_gold_buy: false },
];

const QUOTE = { symbol: '600519.SH', name: '贵州茅台', available: true,
                price: 1.6, change_pct: 2.1, open: 1, high: 2, low: 0.5,
                prev_close: 1.5, amount: 1e8, market_cap: 1e11 };

const FUND = { symbol: '600519.SH', asof: '2026-04-01', available: false,
               hint: '财务数据为空：先执行 lq data financial', score: null, items: [] };

const requestedKeys: string[] = [];

vi.mock('swr', () => ({
  default: (key: string | null) => {
    if (key) requestedKeys.push(key);
    if (!key) return { data: undefined, isLoading: false, error: undefined };
    if (key === '/data/indicators/registry') {
      return { data: REGISTRY, isLoading: false, error: undefined };
    }
    if (key.startsWith('/data/quote')) {
      return { data: QUOTE, isLoading: false, error: undefined };
    }
    if (key.startsWith('/data/indicators?')) {
      return { data: ROWS, isLoading: false, error: undefined };
    }
    if (key.startsWith('/fundamental/score')) {
      return { data: FUND, isLoading: false, error: undefined };
    }
    return { data: [], isLoading: false, error: undefined };
  },
}));

vi.mock('@/lib/api', () => ({ fetcher: vi.fn(), get: vi.fn(), post: vi.fn() }));

vi.mock('next/navigation', () => ({
  useParams: () => ({ symbol: '600519.SH' }),
  useRouter: () => ({ push: vi.fn() }),
}));

// KChart 依赖 lightweight-charts 的 DOM 测量，测试里替换成占位节点
vi.mock('@/components/KChart', () => ({
  default: ({ overlays }: { overlays?: { name: string }[] }) => (
    <div data-testid="kchart">{overlays?.map((o) => o.name).join(',')}</div>
  ),
}));

import SecurityPage from '../[symbol]/page';

const indicatorRequests = () => requestedKeys.filter((k) => k.startsWith('/data/indicators?'));

describe('SecurityPage 指标接线', () => {
  beforeEach(() => {
    requestedKeys.length = 0;
  });

  it('首屏按默认指标集请求，并把可叠加列画到 K 线上', async () => {
    render(<SecurityPage />);
    await waitFor(() => expect(indicatorRequests().length).toBeGreaterThan(0));

    expect(indicatorRequests()[0]).toContain('names=ma,macd,rsi,boll');
    // 只有 pane=price 的数值列叠在价格轴上；macd/rsi/kdj 属 sub 面板不进 K 线
    const chart = await screen.findByTestId('kchart');
    expect(chart.textContent).toContain('ma5');
    expect(chart.textContent).toContain('ma20');
    expect(chart.textContent).toContain('boll_upper');
    expect(chart.textContent).not.toContain('macd_dif');
    expect(chart.textContent).not.toContain('rsi14');
  });

  it('勾选新指标后按新的 names 重新请求', async () => {
    render(<SecurityPage />);
    await waitFor(() => expect(screen.getByLabelText('KDJ')).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText('KDJ'));
    await waitFor(() => {
      expect(indicatorRequests().some((k) => k.includes('names=ma,macd,rsi,boll,kdj')))
        .toBe(true);
    });
  });

  it('取消勾选后 names 收窄', async () => {
    render(<SecurityPage />);
    await waitFor(() => expect(screen.getByLabelText('MACD')).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText('MACD'));
    await waitFor(() => {
      expect(indicatorRequests().some((k) => k.includes('names=ma,rsi,boll'))).toBe(true);
    });
  });

  it('布尔信号列不叠加到 K 线，但在指标条里显示为是/否', async () => {
    render(<SecurityPage />);
    await userEvent.click(await screen.findByLabelText('天道通道（金牛/金钻）'));
    await waitFor(() => expect(indicatorRequests().some((k) => k.includes('tiandao')))
      .toBe(true));
    const chart = await screen.findByTestId('kchart');
    expect(chart.textContent).toContain('td_jinniu');
    expect(chart.textContent).not.toContain('td_gold_buy');
    expect(await screen.findByText('td_gold_buy')).toBeInTheDocument();
  });

  it('挂载基本面卡片并渲染后端 hint', async () => {
    render(<SecurityPage />);
    expect(await screen.findByText('基本面评分')).toBeInTheDocument();
    expect(await screen.findByText(/财务数据为空/)).toBeInTheDocument();
  });
});
