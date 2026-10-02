// 指标选择器 —— 注册表驱动；至少保留一个（names= 为空后端会 422）
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import IndicatorPicker, { isLineOutput, type IndicatorMeta } from '../IndicatorPicker';

const META: IndicatorMeta[] = [
  { name: 'ma', label: '均线族', category: 'trend', pane: 'price', min_window: 60,
    inputs: ['close'], outputs: ['ma5', 'ma20'] },
  { name: 'macd', label: 'MACD', category: 'trend', pane: 'sub', min_window: 60,
    inputs: ['close'], outputs: ['macd_dif', 'macd_hist'] },
  { name: 'rsi', label: 'RSI', category: 'oscillator', pane: 'sub', min_window: 40,
    inputs: ['close'], outputs: ['rsi14'] },
  { name: 'tiandao', label: '天道通道（金牛/金钻）', category: 'channel',
    pane: 'price', min_window: 60,
    inputs: ['high', 'low'], outputs: ['td_jinniu', 'td_gold_buy'] },
];

describe('isLineOutput', () => {
  it('数值列算可画线，布尔列不算', () => {
    const rows = [
      { ma5: 1.5, td_gold_buy: true },
      { ma5: 2.5, td_gold_buy: false },
    ];
    expect(isLineOutput(rows, 'ma5')).toBe(true);
    expect(isLineOutput(rows, 'td_gold_buy')).toBe(false);
  });

  it('空数据一律不算（避免画出一条空线）', () => {
    expect(isLineOutput(undefined, 'ma5')).toBe(false);
    expect(isLineOutput([], 'ma5')).toBe(false);
  });

  it('只看尾部若干行：头部 null、尾部有值的列仍算数值列', () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ ma60: i < 20 ? null : i }));
    expect(isLineOutput(rows, 'ma60')).toBe(true);
  });
});

describe('IndicatorPicker', () => {
  it('按类别分组渲染全部指标', () => {
    render(<IndicatorPicker meta={META} value={['ma']} onChange={vi.fn()} />);
    expect(screen.getByText('趋势')).toBeInTheDocument();
    expect(screen.getByText('摆动')).toBeInTheDocument();
    expect(screen.getByText('通道')).toBeInTheDocument();
    expect(screen.getByLabelText('均线族')).toBeChecked();
    expect(screen.getByLabelText('RSI')).not.toBeChecked();
  });

  it('勾选未选中的指标 → 回调追加', async () => {
    const onChange = vi.fn();
    render(<IndicatorPicker meta={META} value={['ma']} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText('MACD'));
    expect(onChange).toHaveBeenCalledWith(['ma', 'macd']);
  });

  it('取消勾选已选指标 → 回调移除', async () => {
    const onChange = vi.fn();
    render(<IndicatorPicker meta={META} value={['ma', 'macd']} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText('MACD'));
    expect(onChange).toHaveBeenCalledWith(['ma']);
  });

  it('只剩一个时不允许取消（否则 names= 为空会被后端 422）', async () => {
    const onChange = vi.fn();
    render(<IndicatorPicker meta={META} value={['ma']} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText('均线族'));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('标题属性暴露预热根数与输出列，便于排查', () => {
    render(<IndicatorPicker meta={META} value={['ma']} onChange={vi.fn()} />);
    const label = screen.getByText('天道通道（金牛/金钻）').closest('label');
    expect(label).toHaveAttribute('title', expect.stringContaining('td_jinniu'));
    expect(label).toHaveAttribute('title', expect.stringContaining('预热 60'));
  });
});
