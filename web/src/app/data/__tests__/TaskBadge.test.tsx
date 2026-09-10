import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TaskStatusBadge, taskKindText, taskElapsed } from '../TaskBadge';
import { taskStatusText } from '@/lib/format';

describe('TaskStatusBadge 状态→配色/文案映射', () => {
  const CASES: [string, string, string][] = [
    // [status, 文案, class 关键片段]
    ['ok', '完成', 'bg-emerald-50'],
    ['partial', '部分完成', 'bg-amber-50'],
    ['failed', '失败', 'bg-red-50'],
    ['interrupted', '已中断', 'bg-red-50'],
    ['running', '拉取中', 'bg-blue-50'],
    ['pending', '等待中', 'bg-neutral-100'],
  ];

  it.each(CASES)('%s → %s', (status, text, cls) => {
    render(<TaskStatusBadge status={status} />);
    expect(screen.getByText(text).className).toContain(cls);
  });

  it('未知状态退回灰系且原样透出文案（后端新增枚举不空白）', () => {
    render(<TaskStatusBadge status="zombie" />);
    const el = screen.getByText('zombie');
    expect(el.className).toContain('bg-neutral-100');
    expect(taskStatusText('zombie')).toBe('zombie');
  });
});

describe('taskKindText 任务类型文案', () => {
  it('已知类型映射中文，未知原样透出', () => {
    expect(taskKindText('full_backfill')).toBe('全量回填');
    expect(taskKindText('daily_update')).toBe('每日增量');
    expect(taskKindText('mystery_kind')).toBe('mystery_kind');
  });
});

describe('taskElapsed 耗时', () => {
  it('无 started → —', () => {
    expect(taskElapsed(null, '2024-01-01T10:00:00')).toBe('—');
  });

  it('秒级 / 分钟级 / 小时级 / 非法输入', () => {
    expect(taskElapsed('2024-01-01T10:00:00', '2024-01-01T10:00:30')).toBe('30s');
    expect(taskElapsed('2024-01-01T10:00:00', '2024-01-01T10:02:30')).toBe('2m30s');
    expect(taskElapsed('2024-01-01T10:00:00', '2024-01-01T12:00:05')).toBe('2h0m');
  });

  it('running（无 finished）算到当前；finished 早于 started → —', () => {
    expect(taskElapsed('2024-01-01T10:00:00', '2024-01-01T09:00:00')).toBe('—');
    const v = taskElapsed('2024-01-01T10:00:00', null);
    expect(v).toMatch(/^[0-9smh]+$/);
  });
});
