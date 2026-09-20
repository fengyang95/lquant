import { describe, expect, it } from 'vitest';
import { createdText, KIND_TEXT, paramsBrief, STATE_BADGE, STATE_TEXT } from '../types';

describe('paramsBrief 参数摘要', () => {
  it('空入参（null / undefined / 空对象）→ 空串', () => {
    expect(paramsBrief(null)).toBe('');
    expect(paramsBrief(undefined)).toBe('');
    expect(paramsBrief({})).toBe('');
  });

  it('键值对拍平成 "k=v"，对象值 JSON 化', () => {
    expect(paramsBrief({ days: 10 })).toBe('days=10');
    expect(paramsBrief({ formula: 'pct_change_20', top_n: 5 })).toBe('formula=pct_change_20 top_n=5');
    expect(paramsBrief({ values: [5, 10] })).toBe('values=[5,10]');
  });

  it('超过 3 个键 → 前 3 个 + "+N"', () => {
    expect(paramsBrief({ a: 1, b: 2, c: 3, d: 4, e: 5 })).toBe('a=1 b=2 c=3 +2');
    expect(paramsBrief({ a: 1, b: 2, c: 3, d: 4 })).toBe('a=1 b=2 c=3 +1');
    expect(paramsBrief({ a: 1, b: 2, c: 3 })).toBe('a=1 b=2 c=3');
  });

  it('布尔 / null 标量按 String 化；null 不走 JSON 分支', () => {
    expect(paramsBrief({ flag: true, empty: null })).toBe('flag=true empty=null');
  });
});

describe('createdText 时间展示', () => {
  it('ISO 截到分并把 T 换成空格', () => {
    expect(createdText('2024-06-01T10:30:45')).toBe('2024-06-01 10:30');
  });

  it('null → —', () => {
    expect(createdText(null)).toBe('—');
  });

  it('不足 16 位原样截取不报错', () => {
    expect(createdText('2024-06-01')).toBe('2024-06-01');
  });
});

describe('映射表完整性', () => {
  it('KIND_TEXT / STATE_TEXT / STATE_BADGE 覆盖全部枚举键', () => {
    const kinds = ['data', 'sync', 'backtest', 'factor', 'qlib'] as const;
    const states = ['queued', 'running', 'finished', 'failed', 'canceled'] as const;
    for (const k of kinds) expect(KIND_TEXT[k]).toBeTruthy();
    for (const s of states) {
      expect(STATE_TEXT[s]).toBeTruthy();
      expect(STATE_BADGE[s]).toBeTruthy();
    }
    expect(Object.keys(KIND_TEXT)).toHaveLength(kinds.length);
    expect(Object.keys(STATE_TEXT)).toHaveLength(states.length);
    expect(Object.keys(STATE_BADGE)).toHaveLength(states.length);
  });

  it('finished=绿、failed=红、running=蓝（沿用 TaskBadge 语义）', () => {
    expect(STATE_BADGE.finished).toContain('bg-emerald-50');
    expect(STATE_BADGE.failed).toContain('bg-red-50');
    expect(STATE_BADGE.running).toContain('bg-blue-50');
    expect(STATE_BADGE.queued).toContain('bg-neutral-100');
    expect(STATE_BADGE.canceled).toContain('bg-neutral-100');
  });
});
