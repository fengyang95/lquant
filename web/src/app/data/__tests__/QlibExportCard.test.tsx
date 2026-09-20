import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibExportCard from '../QlibExportCard';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

describe('QlibExportCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows missing-data hint when not exported', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({ dir: 'data/qlib', exists: false });
    render(<QlibExportCard />);
    await waitFor(() => screen.getByText(/尚未导出/));
  });

  it('shows manifest calendar when exported', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({
      dir: 'data/qlib',
      exists: true,
      manifest: { symbols: 7212 },
      calendar: { start: '2021-01-04', end: '2026-09-18', days: 1378 },
    });
    render(<QlibExportCard />);
    await waitFor(() => screen.getByText(/2021-01-04/));
  });

  it('submits export and reports job id', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({ dir: 'data/qlib', exists: true });
    vi.mocked(qlib.postQlibExport).mockResolvedValue({ job_id: 'j1' });
    render(<QlibExportCard />);
    const btn = await screen.findByRole('button', { name: /导出/ });
    fireEvent.click(btn);
    await waitFor(() => expect(qlib.postQlibExport).toHaveBeenCalled());
    expect(await screen.findByText(/j1/)).toBeTruthy();
  });

  it('shows error on submit failure', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({ dir: 'data/qlib', exists: true });
    vi.mocked(qlib.postQlibExport).mockRejectedValue(new Error('busy'));
    render(<QlibExportCard />);
    const btn = await screen.findByRole('button', { name: /导出/ });
    fireEvent.click(btn);
    await waitFor(() => screen.getByText(/✗ busy/));
  });
});
