import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QualityPanel from '../QualityPanel';

const checkPayload = {
  summary: { total: 3, fatal: 1, error: 0, warn: 2, info: 0 },
  issues: [
    {
      rule: 'LIMIT_BREACH', severity: 'fatal', detail: '涨停越界 detail',
      dataset: 'daily_bar', symbol: '300001.SZ', trade_date: '2024-05-06', count: 5,
    },
    {
      rule: 'ZOMBIE_BAR', severity: 'warn', detail: '僵尸行 detail',
      dataset: 'daily_bar', symbol: null, trade_date: null, count: 2,
    },
  ],
};

function renderPanel(resp: object = checkPayload, status = 200) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.includes('/data/check') && init?.method === 'POST') {
      return new Response(JSON.stringify(resp), {
        status, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('not found', { status: 404 });
  }));
  return render(<QualityPanel />);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('QualityPanel 全湖质量检查', () => {
  it('初始空态：引导触发，等价 lq data check', () => {
    renderPanel();
    expect(screen.getByText(/尚未检查/)).toBeInTheDocument();
    expect(screen.getByText(/lq data check/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('检查完成：summary 统计 + issue 明细表（fatal 排前）', async () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: '运行全湖检查' }));
    expect(await screen.findByText(/检查完成：3 条 issue/)).toBeInTheDocument();
    expect(screen.getByText(/fatal 1/)).toBeInTheDocument();
    expect(screen.getByText(/warn 2/)).toBeInTheDocument();
    const rows = screen.getAllByRole('row');
    expect(rows).toHaveLength(3); // 表头 + 2 条 issue
    expect(rows[1]).toHaveTextContent('LIMIT_BREACH');
    expect(rows[2]).toHaveTextContent('ZOMBIE_BAR');
    expect(screen.getByText('涨停越界 detail')).toBeInTheDocument();
  });

  it('无 issue：通过提示 + 不渲染明细表', async () => {
    renderPanel({ summary: { total: 0, fatal: 0, error: 0, warn: 0, info: 0 }, issues: [] });
    fireEvent.click(screen.getByRole('button', { name: '运行全湖检查' }));
    expect(await screen.findByText(/质量检查通过/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('后端 500：错误消息透出，不抛未捕获异常', async () => {
    renderPanel({ detail: '质量检查失败: 湖读取异常' }, 500);
    fireEvent.click(screen.getByRole('button', { name: '运行全湖检查' }));
    expect(await screen.findByText(/✗/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '运行全湖检查' })).not.toBeDisabled();
    });
  });
});
