// 缠论面板 —— 有数据 / 降级态 / 加载态三种读法，降级不能阻塞页面其余部分。
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import ChanPanel from '../ChanPanel';
import type { ChanData } from '@/lib/chan';

const OK: ChanData = {
  symbol: '600519.SH',
  freq: '日线',
  available: true,
  asof: '2024-06-28',
  n_bars: 600,
  fx: [{ dt: '2024-06-20', mark: 'G', value: 1700 }],
  bi: [
    { sdt: '2024-05-01', edt: '2024-05-20', direction: 'up',
      high: 1700, low: 1500, n_bars: 12 },
    { sdt: '2024-05-20', edt: '2024-06-28', direction: 'down',
      high: 1700, low: 1580, n_bars: 9 },
  ],
  zs: [{ sdt: '2024-05-20', edt: '2024-06-28', zg: 1680, zd: 1560,
         gg: 1700, dd: 1500, n_bis: 3 }],
  signals: {
    latest: { buy1: '一买', beichi: '上涨', sell3: null },
    events: [
      { dt: '2024-06-01', kind: 'buy1', label: '一买', detail: '9笔' },
      { dt: '2024-06-20', kind: 'sell3', label: '三卖', detail: '' },
    ],
  },
  notes: ['线段未实现：czsc 1.0.1 无线段（XD）实现。'],
};

describe('ChanPanel', () => {
  it('有数据时展示笔数、中枢区间与当前信号', () => {
    render(<ChanPanel data={OK} />);
    expect(screen.getByText('已确认笔数')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('中枢数')).toBeInTheDocument();
    expect(screen.getByText('1560.00 ~ 1680.00')).toBeInTheDocument();
    // 最近一笔 = bi 列表末位（这里是向下笔），不是第一笔
    expect(screen.getByText('向下')).toBeInTheDocument();
    expect(screen.getByText('2024-05-20 → 2024-06-28')).toBeInTheDocument();
    // 「一买」同时出现在信号徽标与事件行里 —— 两处都该有
    expect(screen.getAllByText('一买')).toHaveLength(2);
    expect(screen.getByText('上涨')).toBeInTheDocument();
  });

  it('事件倒序展示，无明细显示占位符', () => {
    render(<ChanPanel data={OK} />);
    const rows = screen.getAllByRole('row').slice(1); // 去掉表头
    expect(rows[0]).toHaveTextContent('2024-06-20');
    expect(rows[1]).toHaveTextContent('2024-06-01');
    expect(rows[0]).toHaveTextContent('—'); // 三卖无明细
  });

  it('渲染后端给的口径说明（线段缺失 / MACD 柱口径）', () => {
    render(<ChanPanel data={OK} />);
    expect(screen.getByText(/线段未实现/)).toBeInTheDocument();
  });

  it('无信号的槽位显示信号名而不是空白', () => {
    render(<ChanPanel data={OK} />);
    // latest 里没有 sell1，槽位应回落到标签「一卖」
    expect(screen.getByText('一卖')).toBeInTheDocument();
  });

  it('available=false 时展示原因而不是报错', () => {
    render(<ChanPanel data={{ symbol: '600519.SH', freq: '日线', available: false,
      reason: '未安装 czsc 依赖，请执行 `uv sync --extra chan` 后重启服务' }} />);
    expect(screen.getByText(/未安装 czsc/)).toBeInTheDocument();
  });

  it('加载态与错误态各有措辞', () => {
    const { unmount } = render(<ChanPanel isLoading />);
    expect(screen.getByText('缠论计算中…')).toBeInTheDocument();
    unmount();
    render(<ChanPanel error={new Error('boom')} />);
    expect(screen.getByText(/缠论加载失败/)).toBeInTheDocument();
  });
});
