import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  COVERAGE_BASE_COLOR, COVERAGE_FLAG_COLOR, COVERAGE_DROP_THRESHOLD,
  flagMonthlyCoverage, lastNMonthsRange, monthlyCoverageOption,
} from '@/lib/coverage';

describe('flagMonthlyCoverage 环比标橙', () => {
  const base = (month: string, avg: number) => ({ month, avg_symbols: avg, days: 20 });

  it('首月无环比：drop=false, drop_pct=null', () => {
    const [r] = flagMonthlyCoverage([base('2024-01', 5000)]);
    expect(r.drop).toBe(false);
    expect(r.drop_pct).toBeNull();
  });

  it('跌幅 >30% 标记为疑似缺口月', () => {
    const rows = flagMonthlyCoverage([base('2024-01', 5000), base('2024-02', 3000)]);
    expect(rows[1].drop_pct).toBeCloseTo(0.4);
    expect(rows[1].drop).toBe(true);
    expect(rows[0].drop).toBe(false);
  });

  it('跌幅恰在阈值内（≤30%）不标记', () => {
    const rows = flagMonthlyCoverage([base('2024-01', 5000), base('2024-02', 3600)]);
    expect(rows[1].drop_pct).toBeCloseTo(0.28);
    expect(rows[1].drop).toBe(false);
  });

  it('阈值精确边界：跌 30.0001% 标记，正好 30% 不标记（严格大于）', () => {
    const rows = flagMonthlyCoverage([
      base('2024-01', 1000), base('2024-02', 699.9999), base('2024-03', 700),
    ], COVERAGE_DROP_THRESHOLD);
    expect(rows[1].drop).toBe(true);
    expect(rows[2].drop).toBe(false);
  });

  it('上涨与持平不标记', () => {
    const rows = flagMonthlyCoverage([
      base('2024-01', 1000), base('2024-02', 1200), base('2024-03', 1200),
    ]);
    expect(rows[1].drop).toBe(false);
    expect(rows[2].drop).toBe(false);
  });

  it('上月为 0 时无法算环比，不标记', () => {
    const rows = flagMonthlyCoverage([base('2024-01', 0), base('2024-02', 100)]);
    expect(rows[1].drop).toBe(false);
    expect(rows[1].drop_pct).toBeNull();
  });

  it('不可变：不修改入参', () => {
    const input = [base('2024-01', 5000), base('2024-02', 3000)];
    const snapshot = input.map((r) => ({ ...r }));
    flagMonthlyCoverage(input);
    expect(input).toEqual(snapshot);
  });
});

describe('monthlyCoverageOption 缺口月柱色', () => {
  it('缺口月用金色警示，正常月用中性靛', () => {
    const rows = flagMonthlyCoverage([
      { month: '2024-01', avg_symbols: 5000, days: 22 },
      { month: '2024-02', avg_symbols: 2000, days: 20 },
    ]);
    const opt = monthlyCoverageOption(rows) as {
      series: { data: { value: number; itemStyle: { color: string } }[] }[];
      xAxis: { data: string[] };
    };
    expect(opt.xAxis.data).toEqual(['2024-01', '2024-02']);
    expect(opt.series[0].data[0].itemStyle.color).toBe(COVERAGE_BASE_COLOR);
    expect(opt.series[0].data[1].itemStyle.color).toBe(COVERAGE_FLAG_COLOR);
  });
});

describe('lastNMonthsRange 默认窗口', () => {
  it('近 24 个月：起点为 24 个月前当月 1 号', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
    const [start, end] = lastNMonthsRange(24, new Date('2026-09-10T08:00:00Z'));
    expect(start).toBe('2024-09-01');
    expect(end).toBe('2026-09-10');
    vi.useRealTimers();
  });
});

afterEach(() => {
  vi.useRealTimers();
});
