import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import LogsPanel from '../LogsPanel';

const items = [
  { ts: '2026-09-16 23:00:00.000', level: 'INFO', run_id: null, source: 'm.f:1', message: 'ok' },
  { ts: '2026-09-16 23:00:01.000', level: 'ERROR', run_id: 'abc', source: 'm.f:2', message: 'boom\nValueError: bad' },
];

function renderPanel(handler?: (url: string) => Response) {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (handler) return handler(url);
    return new Response(JSON.stringify({ code: 0, data: { items, total: 2 }, message: 'ok' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <LogsPanel />
    </SWRConfig>,
  );
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe('LogsPanel', () => {
  it('渲染日志行并可展开多行 message', async () => {
    renderPanel();
    const cell = await screen.findByText(/boom/);
    fireEvent.click(cell);
    expect(cell.className).toContain('whitespace-pre-wrap');
    expect(screen.getByText('m.f:1')).toBeInTheDocument();
  });

  it('级别按钮切换触发带 level 的请求', async () => {
    const fetchMock = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'ERROR' }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('level=ERROR'),
        expect.anything(),
      ),
    );
  });
});
