import { describe, expect, it, vi } from 'vitest';
import {
  buildRunPayload,
  confirmDiscard,
  describeRun,
  formatElapsed,
  isDirty,
  parseFormulas,
  pollDelayMs,
  RUN_POLL_BASE_MS,
  RUN_POLL_MAX_MS,
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

// 运行中反馈：回测实测 26~39s，此前只有一句「执行中…」，用户分不清
// "在正常跑"和"卡死了"，也无法取消。以下覆盖退避节奏与文案。
describe('pollDelayMs（轮询退避）', () => {
  it('首次轮询用基准间隔', () => {
    expect(pollDelayMs(0)).toBe(RUN_POLL_BASE_MS);
  });

  it('随尝试次数单调递增', () => {
    const seq = [0, 1, 2, 3, 4].map(pollDelayMs);
    for (let i = 1; i < seq.length; i++) expect(seq[i]).toBeGreaterThan(seq[i - 1]);
  });

  it('不会超过上限（避免长任务期间请求间隔无限拉长）', () => {
    expect(pollDelayMs(50)).toBe(RUN_POLL_MAX_MS);
    expect(pollDelayMs(999)).toBeLessThanOrEqual(RUN_POLL_MAX_MS);
  });

  it('负数/异常输入回落到基准间隔，不产生 NaN', () => {
    expect(pollDelayMs(-1)).toBe(RUN_POLL_BASE_MS);
    expect(Number.isFinite(pollDelayMs(-5))).toBe(true);
  });
});

describe('formatElapsed（已用时长）', () => {
  it('不足 1 分钟显示秒', () => {
    expect(formatElapsed(0)).toBe('0s');
    expect(formatElapsed(12_400)).toBe('12s');
    expect(formatElapsed(59_999)).toBe('59s');
  });

  it('满 1 分钟显示 mm:ss 且补零', () => {
    expect(formatElapsed(60_000)).toBe('1:00');
    expect(formatElapsed(95_000)).toBe('1:35');
    expect(formatElapsed(600_000)).toBe('10:00');
  });

  it('负数按 0 处理（时钟回拨不显示负时长）', () => {
    expect(formatElapsed(-5000)).toBe('0s');
  });
});

describe('describeRun（运行提示文案）', () => {
  it('排队中带上时长', () => {
    expect(describeRun('queued', null, 3000)).toBe('已提交，排队中… 3s');
  });

  it('有阶段时展示阶段名与时长', () => {
    expect(describeRun('running', { phase: '运行策略' }, 8000)).toBe('运行策略… 8s');
  });

  it('有 done/total 时展示推进量', () => {
    expect(describeRun('running', { phase: '落库', done: 2, total: 5 }, 1000))
      .toBe('落库 2/5… 1s');
  });

  it('无阶段信息时回落到「执行中」而不是空文案', () => {
    expect(describeRun('running', null, 1000)).toBe('执行中… 1s');
    expect(describeRun('running', {}, 1000)).toBe('执行中… 1s');
  });

  it('total 为 0 时不显示 0/0（避免看起来像卡在起点）', () => {
    expect(describeRun('running', { phase: '读取日线', done: 0, total: 0 }, 2000))
      .toBe('读取日线… 2s');
  });
});

describe('confirmDiscard（丢弃保护）', () => {
  it('dirty 为假 → 不打扰用户，直接放行', () => {
    const ask = vi.fn();
    expect(confirmDiscard(false, '新建策略', ask)).toBe(true);
    expect(ask).not.toHaveBeenCalled();
  });

  it('dirty 为真 → 询问并把动作写进文案', () => {
    const ask = vi.fn().mockReturnValue(true);
    expect(confirmDiscard(true, '载入其他策略', ask)).toBe(true);
    expect(ask).toHaveBeenCalledTimes(1);
    expect(ask.mock.calls[0][0]).toContain('载入其他策略');
    expect(ask.mock.calls[0][0]).toContain('未保存');
  });

  it('用户取消 → 返回 false（调用方据此中止丢弃）', () => {
    const ask = vi.fn().mockReturnValue(false);
    expect(confirmDiscard(true, '切换页签', ask)).toBe(false);
  });
});
