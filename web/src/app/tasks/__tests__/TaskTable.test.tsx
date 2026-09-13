// TaskTable 测试 —— 徽章/名称/参数/时间渲染、actionsOf / extraOf 注入、空态与加载态
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import TaskTable, { StateBadge } from '../TaskTable';
import type { TaskItem } from '../types';

const base: TaskItem = {
  id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
  kind: 'backtest',
  name: 'sweep pct_change_20',
  status: 'running',
  state: 'running',
  created_at: '2024-06-01T10:30:45',
  params: { formula: 'pct_change_20', param: 'window', values: [5, 10, 20] },
  error: null,
};

const failed: TaskItem = {
  ...base,
  id: '11111111-bbbb-cccc-dddd-eeeeeeeeeeee',
  name: 'mine run',
  state: 'failed',
  status: 'failed',
  error: 'boom: backend exploded',
};

const tasks: TaskItem[] = [base, failed];

describe('StateBadge', () => {
  const CASES: [TaskItem['state'], string, string][] = [
    ['queued', '排队中', 'bg-neutral-100'],
    ['running', '运行中', 'bg-blue-50'],
    ['finished', '已完成', 'bg-emerald-50'],
    ['failed', '失败', 'bg-red-50'],
    ['canceled', '已取消', 'bg-neutral-100'],
  ];

  it.each(CASES)('%s → %s', (state, text, cls) => {
    render(<StateBadge state={state} />);
    expect(screen.getByText(text).className).toContain(cls);
  });

  it('running 带脉冲点标记', () => {
    const { container } = render(<StateBadge state="running" />);
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('未知状态退回 queued 配色且原样透出（后端新增枚举不空白）', () => {
    render(<StateBadge state={'zombie' as TaskItem['state']} />);
    expect(screen.getByText('zombie').className).toContain('bg-neutral-100');
  });
});

describe('TaskTable', () => {
  it('渲染任务行：名称 / id 前 8 位 / 参数摘要 / 时间截到分', () => {
    render(
      <TaskTable tasks={tasks} loading={false} msg="" emptyHint="空空如也" />,
    );
    expect(screen.getByText('sweep pct_change_20')).toBeInTheDocument();
    expect(screen.getByText('aaaaaaaa')).toBeInTheDocument();
    expect(screen.getAllByText('formula=pct_change_20 param=window values=[5,10,20]')).toHaveLength(2);
    expect(screen.getAllByText('2024-06-01 10:30')).toHaveLength(2);
  });

  it('failed 行透出错误文案（截断展示，title 保留全文）', () => {
    render(<TaskTable tasks={tasks} loading={false} msg="" emptyHint="" />);
    const el = screen.getByText(/boom: backend/);
    expect(el.getAttribute('title')).toBe('boom: backend exploded');
  });

  it('extraOf 注入详情列（表头 + 单元格回调取值）', () => {
    render(
      <TaskTable
        tasks={tasks}
        loading={false}
        msg=""
        emptyHint=""
        extraOf={(t) => String(t.params?.formula ?? '—')}
      />,
    );
    expect(screen.getByText('详情')).toBeInTheDocument();
    expect(screen.getAllByText('pct_change_20').length).toBeGreaterThan(0);
  });

  it('无 extraOf 时不渲染详情列', () => {
    render(<TaskTable tasks={tasks} loading={false} msg="" emptyHint="" />);
    expect(screen.queryByText('详情')).not.toBeInTheDocument();
  });

  it('actionsOf 注入操作按钮并以任务对象回调', async () => {
    const onAction = vi.fn();
    const actionsOf = (t: TaskItem) => (
      <button onClick={() => onAction(t)}>{t.id.slice(0, 4)}</button>
    );
    render(<TaskTable tasks={tasks} loading={false} msg="" emptyHint="" actionsOf={actionsOf} />);
    const user = (await import('@testing-library/user-event')).default.setup();
    await user.click(screen.getByRole('button', { name: 'aaaa' }));
    expect(onAction).toHaveBeenCalledWith(tasks[0]);
  });

  it('空列表 → emptyHint；未取到数据且 loading → 加载中', () => {
    render(<TaskTable tasks={[]} loading={false} msg="" emptyHint="暂无任务" />);
    expect(screen.getByText('暂无任务')).toBeInTheDocument();

    const { unmount } = render(<TaskTable tasks={undefined} loading msg="" emptyHint="" />);
    expect(screen.getByText('加载中…')).toBeInTheDocument();
    unmount();
  });

  it('msg 非空时透出反馈条（✓ 成功 / ✗ 失败配色）', () => {
    render(<TaskTable tasks={[]} loading={false} msg="✗ 出错了" emptyHint="" />);
    expect(screen.getByText('✗ 出错了').className).toContain('text-up');
  });

  it('params 为空 → 参数列显示 —', () => {
    render(
      <TaskTable
        tasks={[{ ...base, params: {} }]}
        loading={false}
        msg=""
        emptyHint=""
      />,
    );
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
