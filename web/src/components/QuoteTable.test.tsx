import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import QuoteTable, { Pct, SymbolLink } from './QuoteTable';

const rows = [
  { symbol: '600519.SH', name: '贵州茅台', close: 1309.3, change_pct: -0.0051, amount: 4.2e9 },
  { symbol: '000001.SZ', name: '平安银行', close: 11.5, change_pct: 0.021, amount: 1.8e9 },
];

describe('QuoteTable', () => {
  it('渲染标的名称与代码', () => {
    render(<QuoteTable rows={rows} />);
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('600519.SH')).toBeInTheDocument();
  });

  it('涨红跌绿：上涨为 text-up，下跌为 text-down', () => {
    render(<QuoteTable rows={rows} />);
    const up = screen.getByText('+2.10%');
    const down = screen.getByText('-0.51%');
    expect(up).toHaveClass('text-up');
    expect(down).toHaveClass('text-down');
  });

  it('成交额自动转亿', () => {
    render(<QuoteTable rows={rows} />);
    expect(screen.getByText('42.00亿')).toBeInTheDocument();
  });

  it('空数据显示占位文案', () => {
    render(<QuoteTable rows={[]} empty="还没加自选" />);
    expect(screen.getByText('还没加自选')).toBeInTheDocument();
  });

  it('showNetInflow=false 时不渲染该列', () => {
    render(<QuoteTable rows={rows} />);
    expect(screen.queryByText('主力净流入')).not.toBeInTheDocument();
  });
});

describe('Pct', () => {
  it('null 显示占位符', () => {
    render(<Pct value={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});

describe('SymbolLink', () => {
  it('无名称时显示占位符', () => {
    render(<SymbolLink symbol="600000.SH" name={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.getByText('600000.SH')).toBeInTheDocument();
  });
});
