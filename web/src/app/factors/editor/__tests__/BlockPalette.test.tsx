/**
 * 积木面板的搜索。
 *
 * 目录实测 45 个算子 + 7 个中缀 + 10 个字段，一面墙的按钮肉眼扫太慢。
 * 搜索要同时命中中文 label 与英文算子名（hint）——用户可能记得 `Ts_Mean`
 * 也可能只记得「均值」，只认一种就会白搜。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Catalog } from '@/lib/factor-canvas/types';
import BlockPalette from '../BlockPalette';

// 只用到拖拽的 MIME 常量，不必把 React Flow 拉进 jsdom
vi.mock('../Canvas', () => ({ BLOCK_DND_TYPE: 'application/x-lquant-block' }));

const CATALOG: Catalog = {
  ops: [
    {
      name: 'Ts_Mean',
      category: 'TS',
      label: '时序均值',
      min_window: 1,
      series_arity: 1,
      params: [{ name: 'n', type: 'window', required: true, default: null }],
    },
    {
      name: 'Ts_Slope',
      category: 'TS',
      label: '时序回归斜率',
      min_window: 1,
      series_arity: 1,
      params: [{ name: 'n', type: 'window', required: true, default: null }],
    },
    {
      name: 'Rank',
      category: 'CS',
      label: '横截面排名',
      min_window: 0,
      series_arity: 1,
      params: [],
    },
  ],
  infix: [
    { token: '+', label: '加法', arity: 2 },
    { token: '-', label: '减法', arity: 2 },
  ],
  fields: [
    { name: 'close', label: '收盘价' },
    { name: 'volume', label: '成交量' },
  ],
};

function setup(onAdd = vi.fn()) {
  render(<BlockPalette catalog={CATALOG} onAdd={onAdd} />);
  return { onAdd, search: screen.getByLabelText('搜索积木') };
}

describe('BlockPalette 搜索', () => {
  it('未搜索：列出字段、常数、中缀与算子', () => {
    setup();
    expect(screen.getByRole('button', { name: '收盘价' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '数字常数' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '加法' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '时序均值' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '横截面排名' })).toBeInTheDocument();
  });

  it('按英文算子名搜索：只留命中的算子，并补显算子名', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, 'Ts_Mean');

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent('时序均值');
    // 搜索时补上算子名，否则按 Ts_Mean 搜出来的按钮只写「均值」，没法确认是哪个
    expect(buttons[0]).toHaveTextContent('Ts_Mean');
    expect(screen.getByText('1 个匹配')).toBeInTheDocument();
  });

  it('按中文搜索：命中对应算子', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, '均值');

    expect(screen.getByRole('button', { name: /时序均值/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /横截面排名/ })).toBeNull();
  });

  it('按字段名搜索：命中字段积木', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, '$close');

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent('收盘价');
  });

  it('大小写不敏感', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, 'rank');
    expect(screen.getByRole('button', { name: /横截面排名/ })).toBeInTheDocument();
  });

  it('搜不到：明说没有匹配，并指向表达式直编', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, '不存在的算子');

    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText('没有匹配')).toBeInTheDocument();
    expect(screen.getByText(/找不到就直接在上方/)).toBeInTheDocument();
  });

  it('Esc 清空搜索，恢复全部积木', async () => {
    const user = userEvent.setup();
    const { search } = setup();

    await user.type(search, '均值');
    expect(screen.getAllByRole('button')).toHaveLength(1);

    await user.type(search, '{Escape}');
    expect(screen.getAllByRole('button').length).toBeGreaterThan(1);
    expect(search).toHaveValue('');
  });

  it('点击搜索结果落块：把种类与算子名交给上层', async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn();
    const { search } = setup(onAdd);

    await user.type(search, 'Ts_Slope');
    await user.click(screen.getByRole('button', { name: /时序回归斜率/ }));

    expect(onAdd).toHaveBeenCalledWith('op', 'Ts_Slope', undefined);
  });

  it('目录为空：提示检查后端，而不是静默空白', () => {
    render(<BlockPalette catalog={{ ops: [], infix: [], fields: [] }} onAdd={vi.fn()} />);
    expect(screen.getByText(/算子目录为空或未加载/)).toBeInTheDocument();
  });
});
