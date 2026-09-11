import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import BackfillModal from '../BackfillModal';
import { post } from '@/lib/api';

vi.mock('@/lib/api', () => ({
  post: vi.fn(),
  fetcher: vi.fn(),
}));

describe('BackfillModal 退市股前置校验引导', () => {
  it('422 提示含「退市股」时展示同步清单引导文案', async () => {
    (post as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('security 表没有退市股 —— 全量回填需含退市标的，请先跑 `lq data reference` 同步标的清单'),
    );
    render(<BackfillModal onClose={() => {}} onCreated={() => {}} />);
    fireEvent.click(screen.getByText('创建任务'));
    await waitFor(() => {
      expect(screen.getByText(/同步全市场清单（含退市）/)).toBeInTheDocument();
    });
  });

  it('普通错误不展示引导', async () => {
    (post as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('日期非法'));
    render(<BackfillModal onClose={() => {}} onCreated={() => {}} />);
    fireEvent.click(screen.getByText('创建任务'));
    await waitFor(() => {
      expect(screen.getByText('日期非法')).toBeInTheDocument();
    });
    expect(screen.queryByText(/同步全市场清单（含退市）/)).not.toBeInTheDocument();
  });
});
