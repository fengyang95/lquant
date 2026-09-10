import { describe, expect, it } from 'vitest';

import { fmtNum, fmtPct, fmtYi, pctColorClass, taskStatusText } from './format';

describe('fmtPct', () => {
  it('正数带加号、负数自带负号', () => {
    expect(fmtPct(0.0123)).toBe('+1.23%');
    expect(fmtPct(-0.0456)).toBe('-4.56%');
    expect(fmtPct(0)).toBe('0.00%');
  });

  it('null / NaN → 占位符', () => {
    expect(fmtPct(null)).toBe('—');
    expect(fmtPct(NaN)).toBe('—');
  });
});

describe('pctColorClass —— A 股约定涨红跌绿', () => {
  it('涨红、跌绿、平灰、空值灰', () => {
    expect(pctColorClass(0.01)).toBe('text-up');
    expect(pctColorClass(-0.01)).toBe('text-down');
    expect(pctColorClass(0)).toBe('text-flat');
    expect(pctColorClass(null)).toBe('text-flat');
  });
});

describe('fmtYi', () => {
  it('元转亿', () => {
    expect(fmtYi(1.5e9)).toBe('15.00亿');
  });

  it('超万亿自动换档', () => {
    expect(fmtYi(2e12)).toBe('2.00万亿');
    expect(fmtYi(2e13)).toBe('20.00万亿');
  });

  it('null / NaN → 占位符', () => {
    expect(fmtYi(undefined)).toBe('—');
  });
});

describe('taskStatusText —— 任务状态中文文案', () => {
  it('已知状态映射', () => {
    expect(taskStatusText('pending')).toBe('等待中');
    expect(taskStatusText('running')).toBe('拉取中');
    expect(taskStatusText('ok')).toBe('完成');
    expect(taskStatusText('partial')).toBe('部分完成');
    expect(taskStatusText('failed')).toBe('失败');
    expect(taskStatusText('interrupted')).toBe('已中断');
  });

  it('未知状态原样返回', () => {
    expect(taskStatusText('weird')).toBe('weird');
  });
});

describe('fmtNum', () => {
  it('按位数格式化', () => {
    expect(fmtNum(1309.305, 2)).toBe('1309.31');
    expect(fmtNum(0, 2)).toBe('0.00');
    expect(fmtNum(null)).toBe('—');
  });
});
