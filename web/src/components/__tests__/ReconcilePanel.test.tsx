// 三表勾稽面板 —— 端点此前前端零引用，且要求调用方手填七个科目（等于不可用）。
// 这里固定住：默认自动取数、显式覆盖优先、以及**口径局限必须写在界面上**。
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const postMock = vi.fn();

vi.mock('@/lib/api', () => ({
  post: (...a: unknown[]) => postMock(...a),
  get: vi.fn(),
  fetcher: vi.fn(),
}));

import ReconcilePanel from '../ReconcilePanel';

const OK = {
  symbol: '600519.SH',
  passed: false,
  score: 9,
  full_score: 14,
  items: [
    { name: 'retained_earnings', gap: 0.31, passed: false, score: 0,
      note: '|NI+OCI−ΔRE|/|NI|' },
    { name: 'cash_change', gap: 0.03, passed: true, score: 5,
      note: '|CF净变动−货币资金变动|/max(|·|)' },
    { name: 'earnings_quality', gap: 0.01, passed: true, score: 4,
      note: '|NI−扣非|/|NI|' },
  ],
  inputs_meta: {
    asof: '2026-10-06',
    auto: true,
    latest_stat_date: '2026-06-30',
    previous_stat_date: '2026-03-31',
    period_basis: 'quarterly',
    from_request: [],
    from_financial_pit: ['net_income', 'operating_cashflow'],
  },
};

describe('ReconcilePanel', () => {
  beforeEach(() => {
    postMock.mockReset();
    postMock.mockResolvedValue(OK);
  });

  it('初始状态给出可操作的空态提示', () => {
    render(<ReconcilePanel />);
    expect(screen.getByText(/默认从最近两期报表自动取数/)).toBeInTheDocument();
  });

  it('未填代码时不发请求并给出提示', async () => {
    render(<ReconcilePanel />);
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));
    expect(await screen.findByText('请填写股票代码')).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it('默认走自动取数（auto=true）且不携带任何手工字段', async () => {
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    const [path, body] = postMock.mock.calls[0];
    expect(path).toBe('/fundamental/reconcile');
    expect(body).toMatchObject({ symbol: '600519.SH', auto: true });
    // 七个数值项都不该出现 —— 出现就意味着用空值覆盖了自动取数
    expect(Object.keys(body)).toEqual(expect.arrayContaining(['symbol', 'auto']));
    expect(body.net_income).toBeUndefined();
  });

  it('渲染勾稽项、得分与取数来源', async () => {
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));

    expect(await screen.findByText('留存收益勾稽')).toBeInTheDocument();
    expect(screen.getByText('现金变动勾稽')).toBeInTheDocument();
    expect(screen.getByText('利润质量')).toBeInTheDocument();
    expect(screen.getByText('31.0%')).toBeInTheDocument();
    expect(screen.getByText('9 / 14')).toBeInTheDocument();
    expect(screen.getByText(/2026-06-30 ← 2026-03-31/)).toBeInTheDocument();
    expect(screen.getByText(/自动取数项：net_income/)).toBeInTheDocument();
  });

  it('把口径局限写在界面上（分红会天然造成留存收益差额）', async () => {
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));
    expect(await screen.findByText(/口径局限/)).toBeInTheDocument();
    expect(screen.getByText(/分红较多的公司这一项会天然报出较大差额/))
      .toBeInTheDocument();
  });

  it('手工覆盖：只有填了值的字段才随请求发出', async () => {
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '手工覆盖取值' }));
    fireEvent.change(screen.getByLabelText('净利润'), { target: { value: '123' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    const body = postMock.mock.calls[0][1];
    expect(body.net_income).toBe(123);
    expect(body.deducted_net_income).toBeUndefined();
  });

  it('请求失败时显示错误而不是静默', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });

  it('一项都没查到时不判定通过', async () => {
    postMock.mockResolvedValue({ ...OK, items: [], passed: false, score: 0 });
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));
    expect(await screen.findByText(/数据缺失不能被当成质量优秀/))
      .toBeInTheDocument();
  });

  it('自动取数失败时把原因显示出来', async () => {
    postMock.mockResolvedValue({
      ...OK,
      items: [],
      inputs_meta: { ...OK.inputs_meta, auto_error: '该标的没有可用报表',
                     from_financial_pit: [] },
    });
    render(<ReconcilePanel />);
    fireEvent.change(screen.getByLabelText('勾稽股票代码'),
                     { target: { value: '600519.SH' } });
    fireEvent.click(screen.getByRole('button', { name: '开始勾稽' }));
    expect(await screen.findByText(/该标的没有可用报表/)).toBeInTheDocument();
  });
});
