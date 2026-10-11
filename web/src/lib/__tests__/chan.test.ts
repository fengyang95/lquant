// 缠论几何纯函数 —— 画错一笔的代价是用户看错结构，比崩溃更难发现，所以逐条钉死。
import { describe, expect, it } from 'vitest';

import {
  biEndpoints,
  clampSpan,
  eventAnchor,
  fxTriangle,
  signalColor,
  signalTone,
  SIGNAL_LABELS,
  SIGNAL_ORDER,
  type ChanBi,
} from '../chan';

const UP: ChanBi = { sdt: '2024-01-01', edt: '2024-01-10', direction: 'up',
  high: 12, low: 8, n_bars: 7 };
const DOWN: ChanBi = { ...UP, direction: 'down' };

describe('biEndpoints', () => {
  it('向上笔从低点走到高点', () => {
    expect(biEndpoints(UP)).toEqual({ sPrice: 8, ePrice: 12 });
  });

  it('向下笔从高点走到低点', () => {
    expect(biEndpoints(DOWN)).toEqual({ sPrice: 12, ePrice: 8 });
  });
});

describe('clampSpan', () => {
  it('两端都在视口内时原样返回', () => {
    expect(clampSpan(10, 90, 100)).toEqual({ x1: 10, x2: 90 });
  });

  it('两端都不可见时整体不画', () => {
    expect(clampSpan(null, null, 100)).toBeNull();
  });

  it('只有左端不可见时夹到视口左边界（中枢常比视口宽）', () => {
    expect(clampSpan(null, 60, 100)).toEqual({ x1: 0, x2: 60 });
  });

  it('只有右端不可见时夹到视口右边界', () => {
    expect(clampSpan(20, null, 100)).toEqual({ x1: 20, x2: 100 });
  });

  it('两端顺序颠倒时自动摆正（不产生负宽度矩形）', () => {
    expect(clampSpan(80, 20, 100)).toEqual({ x1: 20, x2: 80 });
  });
});

describe('fxTriangle', () => {
  it('顶分型画在价格上方，尖端贴着极值点', () => {
    const [apex, left, right] = fxTriangle(50, 100, 'G', 5);
    expect(apex).toEqual([50, 100]);
    expect(left[1]).toBeLessThan(100); // 底边在上方
    expect(right[1]).toBeLessThan(100);
    expect(left[0]).toBeLessThan(right[0]);
  });

  it('底分型画在价格下方', () => {
    const [apex, left] = fxTriangle(50, 100, 'D', 5);
    expect(apex).toEqual([50, 100]);
    expect(left[1]).toBeGreaterThan(100);
  });

  it('尺寸可调且左右对称', () => {
    const [, left, right] = fxTriangle(50, 100, 'G', 8);
    expect(left[0]).toBe(42);
    expect(right[0]).toBe(58);
  });
});

describe('eventAnchor', () => {
  const bar = { high: 15, low: 5 };
  it('买点取低点（画在 K 线下方）', () => {
    expect(eventAnchor('buy1', bar)).toBe(5);
    expect(eventAnchor('buy3', bar)).toBe(5);
  });
  it('卖点取高点（画在 K 线上方）', () => {
    expect(eventAnchor('sell2', bar)).toBe(15);
  });
  it('背驰不是买也不是卖，落到低点一侧', () => {
    expect(eventAnchor('beichi', bar)).toBe(5);
  });
});

describe('信号配色', () => {
  it('买点用涨色、卖点用跌色、背驰用中性色', () => {
    expect(signalTone('buy1')).toBe('up');
    expect(signalTone('sell3')).toBe('down');
    expect(signalTone('beichi')).toBe('neutral');
    expect(signalColor('buy1')).toBe('#C3352B');
    expect(signalColor('sell1')).toBe('#1E7C55');
    expect(signalColor('beichi')).toBe('#B08A3E');
  });

  it('每种信号都有中文名，且顺序表覆盖全部种类', () => {
    for (const k of SIGNAL_ORDER) {
      expect(SIGNAL_LABELS[k]).toBeTruthy();
    }
    expect(new Set(SIGNAL_ORDER).size).toBe(SIGNAL_ORDER.length);
  });
});
