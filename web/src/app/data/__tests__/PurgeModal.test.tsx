import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PurgeModal from '../PurgeModal';

vi.mock('@/lib/api', () => ({
  post: vi.fn(),
}));

import { post } from '@/lib/api';

const postMock = post as ReturnType<typeof vi.fn>;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  postMock.mockReset();
});

function fillSymbols() {
  fireEvent.change(screen.getByPlaceholderText('000001.SZ, 600000.SH'), {
    target: { value: '000001.SZ' },
  });
}

describe('PurgeModal 数据清理', () => {
  it('无过滤条件点预览：本地报错不发请求', async () => {
    render(<PurgeModal onClose={() => {}} onPurged={() => {}} />);
    fireEvent.click(screen.getByText('预览'));
    expect(await screen.findByText(/至少填写一个过滤条件/)).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it('预览 dry_run=true 显示将删行数；预览后「确认删除」解禁', async () => {
    postMock.mockResolvedValue({ dry_run: true, rows_matched: 1234 });
    render(<PurgeModal onClose={() => {}} onPurged={() => {}} />);
    expect(screen.getByText('确认删除').closest('button')).toBeDisabled();
    fillSymbols();
    fireEvent.click(screen.getByText('预览'));
    expect(await screen.findByText(/将删除/)).toBeInTheDocument();
    expect(screen.getByText('1,234')).toBeInTheDocument();
    expect(screen.getByText('确认删除').closest('button')).not.toBeDisabled();
  });

  it('确认删除：confirm 二次确认 → dry_run=false → onPurged 回调', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    postMock.mockImplementation(async (_url: string, bodyArg?: Record<string, unknown>) => {
      return bodyArg?.dry_run ? { dry_run: true, rows_matched: 1234 } : { dry_run: false, rows_matched: 1234 };
    });
    const onPurged = vi.fn();
    render(<PurgeModal onClose={() => {}} onPurged={onPurged} />);
    fillSymbols();
    fireEvent.click(screen.getByText('预览'));
    await screen.findByText(/将删除/);
    const btn = screen.getByText('确认删除').closest('button') as HTMLButtonElement;
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(onPurged).toHaveBeenCalledWith({ dry_run: false, rows_matched: 1234 });
    });
    const bodies = postMock.mock.calls.map((c) => c[1] as Record<string, unknown>);
    expect(bodies).toHaveLength(2);
    expect(bodies[0].dry_run).toBe(true);
    expect(bodies[1].dry_run).toBe(false);
    expect(bodies[1].symbols).toEqual(['000001.SZ']);
  });

  it('取消二次确认：不发删除请求', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    postMock.mockResolvedValue({ dry_run: true, rows_matched: 10 });
    const onPurged = vi.fn();
    render(<PurgeModal onPurged={onPurged} onClose={() => {}} />);
    fillSymbols();
    fireEvent.click(screen.getByText('预览'));
    await screen.findByText(/将删除/);
    fireEvent.click(screen.getByText('确认删除'));
    await waitFor(() => {
      expect(postMock.mock.calls.filter((c) => (c[1] as Record<string, unknown>).dry_run === false))
        .toHaveLength(0);
    });
    expect(onPurged).not.toHaveBeenCalled();
  });

  it('后端 422（无过滤条件兜底）：错误透出', async () => {
    postMock.mockRejectedValue(new Error('422 至少需要一个过滤条件'));
    render(<PurgeModal onClose={() => {}} onPurged={() => {}} />);
    fillSymbols();
    fireEvent.click(screen.getByText('预览'));
    expect(await screen.findByText(/422/)).toBeInTheDocument();
  });
});
