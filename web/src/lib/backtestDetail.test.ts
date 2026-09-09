import { describe, expect, it } from 'vitest';

import {
  customChartOption,
  recordChartOption,
  type CustomChartItem,
  type RecordPt,
} from './backtestDetail';

type Opt = {
  xAxis: { data: string[] };
  legend: { data: string[] };
  series: { name: string; data: (number | null)[] }[];
};
const asOpt = (o: object | null) => o as Opt | null;

const pts: RecordPt[] = [
  { date: '2024-01-01', value: 1 },
  { date: '2024-01-02', value: 1.5 },
  { date: '2024-01-03', value: 2 },
];

describe('recordChartOption', () => {
  it('构建 category x 轴 + 单系列折线 option', () => {
    const opt = asOpt(recordChartOption('leverage', pts));
    expect(opt).not.toBeNull();
    expect(opt!.xAxis.data).toEqual(['2024-01-01', '2024-01-02', '2024-01-03']);
    expect(opt!.series).toHaveLength(1);
    expect(opt!.series[0].name).toBe('leverage');
    expect(opt!.series[0].data).toEqual([1, 1.5, 2]);
  });

  it('数值保留 6 位小数、非有限值转 null', () => {
    const opt = asOpt(recordChartOption('x', [
      { date: '2024-01-01', value: 0.123456789 },
      { date: '2024-01-02', value: NaN },
    ]));
    expect(opt!.series[0].data).toEqual([0.123457, null]);
  });

  it('空/undefined 数据返回 null', () => {
    expect(recordChartOption('k', [])).toBeNull();
    expect(recordChartOption('k', undefined)).toBeNull();
  });
});

describe('customChartOption', () => {
  it('按 x/ys 字段映射多系列', () => {
    const item: CustomChartItem = {
      type: 'chart',
      data: [
        { d: '2024-01-01', a: 1, b: 2 },
        { d: '2024-01-02', a: 3, b: 'not-num' },
      ],
      x: 'd',
      ys: ['a', 'b'],
    };
    const opt = asOpt(customChartOption(item));
    expect(opt).not.toBeNull();
    expect(opt!.xAxis.data).toEqual(['2024-01-01', '2024-01-02']);
    expect(opt!.series).toHaveLength(2);
    expect(opt!.series[0].data).toEqual([1, 3]);
    expect(opt!.series[1].data).toEqual([2, null]);
    expect(opt!.legend.data).toEqual(['a', 'b']);
  });

  it('空数据 / 缺 x / 空 ys 返回 null', () => {
    expect(customChartOption({ type: 'chart', data: [], x: 'd', ys: ['a'] })).toBeNull();
    expect(customChartOption({ type: 'chart', data: [{ d: 'x' }], x: '', ys: ['a'] })).toBeNull();
    expect(customChartOption({ type: 'chart', data: [{ d: 'x' }], x: 'd', ys: [] })).toBeNull();
  });
});
