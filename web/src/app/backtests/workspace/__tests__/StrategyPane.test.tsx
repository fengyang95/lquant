// StrategyPane 测试 —— 载入回调 / 删除确认与取消 / 空态文案
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import StrategyPane from '../StrategyPane';
import type { StrategyMeta } from '../state';

const strategies: StrategyMeta[] = [
  { id: 'a', name: '双均线', source: 'user', description: 'MA5/MA20 交叉' },
  { id: 'b', name: '动量', source: 'user' },
];

describe('StrategyPane', () => {
  it('点击策略名触发 onLoad', () => {
    const onLoad = vi.fn();
    render(<StrategyPane strategies={strategies} selectedId={null} onLoad={onLoad} onDelete={vi.fn()} onNew={vi.fn()} />);
    fireEvent.click(screen.getByText('双均线'));
    expect(onLoad).toHaveBeenCalledWith('a');
  });

  it('删除需确认：确定触发 onDelete(id, name)，取消不触发', () => {
    const onDelete = vi.fn();
    render(<StrategyPane strategies={strategies} selectedId={null} onLoad={vi.fn()} onDelete={onDelete} onNew={vi.fn()} />);
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    expect(screen.getByText('确认删除？')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确定' }));
    expect(onDelete).toHaveBeenCalledWith('a', '双均线');
  });

  it('空策略列表显示空态文案，且仍可新建', () => {
    const onNew = vi.fn();
    render(<StrategyPane strategies={[]} selectedId={null} onLoad={vi.fn()} onDelete={vi.fn()} onNew={onNew} />);
    expect(screen.getByText('策略库还是空的 —— 点「新建」写一个')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '+ 新建策略' }));
    expect(onNew).toHaveBeenCalled();
  });
});
