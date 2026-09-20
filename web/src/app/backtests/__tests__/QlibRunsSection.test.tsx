import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibRunsSection from '../QlibRunsSection';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

const RUN = (id: string, over: Partial<qlib.QlibRun> = {}): qlib.QlibRun => ({
  id, config: 'a.yaml', market: null, exp_name: 'e', status: 'finished',
  metrics: { IC: 0.01, ICIR: 0.1 }, config_snapshot: null, log_path: null,
  created_at: '2026-09-19T00:00:00+00:00', finished_at: null, error: null,
  ...over,
});

describe('QlibRunsSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders metric rows', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([RUN('a1')]);
    render(<QlibRunsSection />);
    await waitFor(() => screen.getByText('0.0100'));
  });

  it('compare view shows both values', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([RUN('a1'), RUN('b2')]);
    vi.mocked(qlib.compareQlibRuns).mockResolvedValue({
      runs: [
        { id: 'a1', config: 'a.yaml', exp_name: 'e', status: 'finished' },
        { id: 'b2', config: 'b.yaml', exp_name: 'e', status: 'finished' },
      ],
      rows: [{ metric: 'IC', a1: 0.01, b2: 0.03 }],
    });
    render(<QlibRunsSection />);
    const boxes = await screen.findAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    fireEvent.click(screen.getByRole('button', { name: '对比' }));
    await waitFor(() => screen.getAllByText('0.0300').length > 0);
  });

  it('cancel calls api', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([RUN('a1', { status: 'running' })]);
    vi.mocked(qlib.cancelQlibRun).mockResolvedValue({ canceled: true });
    render(<QlibRunsSection />);
    const btn = await screen.findByRole('button', { name: '取消' });
    fireEvent.click(btn);
    await waitFor(() => expect(qlib.cancelQlibRun).toHaveBeenCalledWith('a1'));
  });
});
