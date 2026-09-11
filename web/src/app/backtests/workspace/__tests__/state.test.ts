import { describe, expect, it } from 'vitest';
import {
  buildRunPayload,
  isDirty,
  parseFormulas,
  type EditorParams,
  type Snapshot,
} from '../state';

describe('parseFormulas', () => {
  it('逗号分隔、trim、去空项', () => {
    expect(parseFormulas(' a , b,,c ')).toEqual(['a', 'b', 'c']);
  });

  it('空串返回空数组', () => {
    expect(parseFormulas('')).toEqual([]);
  });
});

describe('buildRunPayload', () => {
  const params: EditorParams = { start: '2024-01-01', end: '2024-06-30', formulas: 'a,b' };

  it('带 strategy_id 与 end 时输出完整对象', () => {
    expect(buildRunPayload('print(1)', params, 's1')).toEqual({
      code: 'print(1)',
      start: '2024-01-01',
      end: '2024-06-30',
      factor_formulas: ['a', 'b'],
      strategy_id: 's1',
    });
  });

  it('strategyId=null、end 为空串时省略 end 与 strategy_id 键', () => {
    const payload = buildRunPayload('print(1)', { ...params, end: '' }, null);
    expect(payload).toEqual({
      code: 'print(1)',
      start: '2024-01-01',
      factor_formulas: ['a', 'b'],
    });
    expect('end' in payload).toBe(false);
    expect('strategy_id' in payload).toBe(false);
  });
});

describe('isDirty', () => {
  const base: Snapshot = {
    name: 'n',
    description: 'd',
    code: 'c',
    params: { start: '2024-01-01', end: '2024-06-30', formulas: 'a' },
  };

  it('与 base 完全相同 → false', () => {
    expect(isDirty(base, base)).toBe(false);
  });

  it('改 code → true', () => {
    expect(isDirty({ ...base, code: 'x' }, base)).toBe(true);
  });

  it('改 params.start → true', () => {
    expect(isDirty({ ...base, params: { ...base.params, start: '2024-02-01' } }, base)).toBe(true);
  });

  it('base 为 null 且全空 → false', () => {
    const empty: Snapshot = {
      name: '',
      description: '',
      code: '',
      params: { start: '', end: '', formulas: '' },
    };
    expect(isDirty(empty, null)).toBe(false);
  });

  it('base 为 null 且 code 非空 → true', () => {
    const empty: Snapshot = {
      name: '',
      description: '',
      code: 'c',
      params: { start: '', end: '', formulas: '' },
    };
    expect(isDirty(empty, null)).toBe(true);
  });

  it('base 为 null 且 name 非空 → true', () => {
    const empty: Snapshot = {
      name: 'n',
      description: '',
      code: '',
      params: { start: '', end: '', formulas: '' },
    };
    expect(isDirty(empty, null)).toBe(true);
  });
});
