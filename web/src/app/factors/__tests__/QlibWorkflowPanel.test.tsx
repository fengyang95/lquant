import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibWorkflowPanel from '../QlibWorkflowPanel';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

const CFGS = [{ name: 'wf_a', path: 'x', mtime: 1 }];

const RUN: qlib.QlibRun = {
  id: 'r1', config: 'wf_a.yaml', market: null, exp_name: 'e',
  status: 'finished', metrics: { IC: 0.03 }, config_snapshot: null,
  log_path: null, created_at: '2026-09-20T00:00:00+00:00',
  finished_at: null, error: null,
};

describe('QlibWorkflowPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(qlib.getQlibConfig).mockResolvedValue({ name: 'wf_a', content: 'qlib_init: {}\n' });
  });

  it('renders config list and runs', async () => {
    vi.mocked(qlib.listQlibConfigs).mockResolvedValue(CFGS);
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([RUN]);
    render(<QlibWorkflowPanel />);
    await waitFor(() => screen.getByText('wf_a'));
    expect(screen.getByText(/r1/)).toBeTruthy();
  });

  it('runs workflow with selected config', async () => {
    vi.mocked(qlib.listQlibConfigs).mockResolvedValue(CFGS);
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([]);
    vi.mocked(qlib.postQlibWorkflow).mockResolvedValue({ run_id: 'r9' });
    render(<QlibWorkflowPanel />);
    const btn = await screen.findByRole('button', { name: '运行' });
    fireEvent.click(btn);
    await waitFor(() =>
      expect(qlib.postQlibWorkflow).toHaveBeenCalledWith(
        expect.objectContaining({ config: 'wf_a' })));
  });

  it('saves config content', async () => {
    vi.mocked(qlib.listQlibConfigs).mockResolvedValue(CFGS);
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([]);
    vi.mocked(qlib.getQlibConfig).mockResolvedValue({ name: 'wf_a', content: 'qlib_init: {}\n' });
    vi.mocked(qlib.putQlibConfig).mockResolvedValue({ saved: true });
    render(<QlibWorkflowPanel />);
    await screen.findByRole('button', { name: '保存配置' });
    const ta = document.querySelector('textarea') as HTMLTextAreaElement | null;
    await waitFor(() =>
      expect(ta).not.toBeNull());
    await waitFor(() =>
      expect((ta as HTMLTextAreaElement).value).toBe('qlib_init: {}\n'));
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() =>
      expect(qlib.putQlibConfig).toHaveBeenCalledWith('wf_a', 'qlib_init: {}\n'));
  });

  it('配置/运行加载失败时显示可见错误，而不是空白页', async () => {
    vi.mocked(qlib.listQlibConfigs).mockRejectedValue(new Error('boom'));
    vi.mocked(qlib.listQlibRuns).mockRejectedValue(new Error('boom2'));
    render(<QlibWorkflowPanel />);
    await waitFor(() => expect(screen.getByText(/加载失败/)).toBeTruthy());
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });
});
