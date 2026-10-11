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
                source: 'realtime', price: 1.6, change_pct: 2.1, open: 1,
                high: 2, low: 0.5, prev_close: 1.5, amount: 1e8,
                market_cap: 1e11 };

/** 实时不可用时的湖内兜底响应（后端 /data/quote 的 source='daily' 分支） */
const QUOTE_DAILY = { symbol: '600519.SH', available: false, source: 'daily',
                      price: 1.6, change_pct: 2.1, open: 8, high: 9, low: 7,
                      prev_close: 1.5, amount: 4.45e9, market_cap: 1.56e12,
                      trade_date: '2026-06-30', mv_date: '2026-06-26' };

/** 实时与湖内都没有（source=null）：价格退回日线末根，额 / 市值显示 — */
const QUOTE_EMPTY = { symbol: '600519.SH', available: false, source: null };

const FUND = { symbol: '600519.SH', asof: '2026-04-01', available: false,
               hint: '财务数据为空：先执行 lq data financial', score: null, items: [] };

const CHAN = {
  symbol: '600519.SH', freq: '日线', available: true, asof: '2026-06-30', n_bars: 600,
  fx: [{ dt: '2026-06-20', mark: 'G', value: 2 }],
  bi: [{ sdt: '2026-06-01', edt: '2026-06-20', direction: 'up',
         high: 2, low: 1, n_bars: 9 }],
  zs: [{ sdt: '2026-06-01', edt: '2026-06-30', zg: 1.9, zd: 1.1,
         gg: 2, dd: 1, n_bis: 3 }],
  signals: { latest: { buy1: '一买' }, events: [
    { dt: '2026-06-20', kind: 'buy1', label: '一买', detail: '9笔' }] },
  notes: ['线段未实现：czsc 1.0.1 无线段（XD）实现。'],
};

const requestedKeys: string[] = [];
/** 可变夹具：实时 / 兜底 / 全空三条报价分支不必重载模块即可覆盖 */
let quoteData: typeof QUOTE | typeof QUOTE_DAILY | typeof QUOTE_EMPTY = QUOTE;

vi.mock('swr', () => ({
  default: (key: string | null) => {
    if (key) requestedKeys.push(key);
    if (!key) return { data: undefined, isLoading: false, error: undefined };
    if (key === '/data/indicators/registry') {
      return { data: REGISTRY, isLoading: false, error: undefined };
    }
    if (key.startsWith('/data/quote')) {
      return { data: quoteData, isLoading: false, error: undefined };
    }
    if (key.startsWith('/data/indicators?')) {
      return { data: ROWS, isLoading: false, error: undefined };
    }
    if (key.startsWith('/fundamental/score')) {
      return { data: FUND, isLoading: false, error: undefined };
    }
    if (key.includes('/chan')) {
      return { data: CHAN, isLoading: false, error: undefined };
    }
    return { data: [], isLoading: false, error: undefined };
  },
}));

vi.mock('@/lib/api', () => ({ fetcher: vi.fn(), get: vi.fn(), post: vi.fn() }));

vi.mock('next/navigation', () => ({
  useParams: () => ({ symbol: '600519.SH' }),
  useRouter: () => ({ push: vi.fn() }),
}));

// KChart 依赖 lightweight-charts 的 DOM 测量，测试里替换成占位节点。
// 顺带把 chan 是否传入暴露出来（叠加层本身在浏览器里画，测试只验接线）。
vi.mock('@/components/KChart', () => ({
  default: ({ overlays, chan }: {
    overlays?: { name: string }[];
    chan?: { available: boolean };
  }) => (
    <div data-testid="kchart" data-chan={chan ? String(chan.available) : 'none'}>
      {overlays?.map((o) => o.name).join(',')}
    </div>
  ),
}));

import SecurityPage from '../[symbol]/page';

const indicatorRequests = () => requestedKeys.filter((k) => k.startsWith('/data/indicators?'));

describe('SecurityPage 指标接线', () => {
  beforeEach(() => {
    requestedKeys.length = 0;
    quoteData = QUOTE;
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

  it('按 symbol 拉取缠论结构，并把数据同时给到面板与 K 线叠加层', async () => {
    render(<SecurityPage />);
    await waitFor(() => {
      expect(requestedKeys.some((k) => k.includes('/chan?limit=250'))).toBe(true);
    });
    expect(await screen.findByText('缠论结构')).toBeInTheDocument();
    expect(await screen.findByText('已确认笔数')).toBeInTheDocument();
    expect(await screen.findByText('1.10 ~ 1.90')).toBeInTheDocument();
    // K 线叠加层拿到的是同一份 chan（available=true）
    const chart = await screen.findByTestId('kchart');
    await waitFor(() => expect(chart.getAttribute('data-chan')).toBe('true'));
  });
});

describe('SecurityPage 报价口径（实时 / 湖内兜底）', () => {
  beforeEach(() => {
    requestedKeys.length = 0;
    quoteData = QUOTE;
  });

  it('实时可用：标「实时」并给出开高低昨收', async () => {
    render(<SecurityPage />);
    expect(await screen.findByText(/实时 · 开 1\.00/)).toBeInTheDocument();
    expect(screen.getByText('1.00亿')).toBeInTheDocument();     // 成交额
    expect(screen.getByText('1000.00亿')).toBeInTheDocument();  // 总市值
  });

  it('实时不可用：成交额 / 总市值取湖内值而不是「—」，并标注口径日', async () => {
    quoteData = QUOTE_DAILY;
    render(<SecurityPage />);

    expect(await screen.findByText(/实时行情不可用 · 湖内日线 2026-06-30/))
      .toBeInTheDocument();
    expect(screen.getByText('44.50亿')).toBeInTheDocument();      // 成交额（湖内）
    expect(screen.getByText('1.56万亿')).toBeInTheDocument();     // 总市值（湖内）
    // 市值口径日单列：估值湖滞后于日线，不能混成一个日期
    expect(screen.getByText('口径 2026-06-26')).toBeInTheDocument();
    expect(screen.getByText('口径 2026-06-30')).toBeInTheDocument();
    expect(screen.getAllByText('2026-06-30').length).toBeGreaterThan(0);  // 数据截至
  });

  it('实时与湖内都没有时如实显示「—」，并用日线末根兜底价格', async () => {
    quoteData = QUOTE_EMPTY;
    render(<SecurityPage />);

    expect(await screen.findByText(/实时行情不可用/)).toBeInTheDocument();
    // 成交额 / 总市值两栏都是 —（不编 0）
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
    // 价格退回 K 线末根（ROWS 末根 close=1.6），仍有一个数可看
    expect(screen.getByText('1.60')).toBeInTheDocument();
  });
});
