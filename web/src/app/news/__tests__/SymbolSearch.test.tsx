import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { SymbolSearch } from '../SymbolSearch';

function renderSearch(onSelect: (s: string) => void) {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <SymbolSearch onSelect={onSelect} />
    </SWRConfig>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('SymbolSearch', () => {
  it('输入 ≥2 字符防抖后调 /data/securities，点击建议回调 symbol 并清空输入', async () => {
    const onSelect = vi.fn();
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        calls.push(url);
        return new Response(
          JSON.stringify([{ symbol: '600519.SH', name: '贵州茅台', sec_type: 'stock' }]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }),
    );
    renderSearch(onSelect);

    fireEvent.change(screen.getByPlaceholderText(/输入代码或名称/), {
      target: { value: '600519' },
    });
    const sug = await screen.findByText('贵州茅台');
    expect(calls.some((u) => u.includes('/data/securities?q=600519'))).toBe(true);

    fireEvent.click(sug);
    expect(onSelect).toHaveBeenCalledWith('600519.SH');
    expect((screen.getByPlaceholderText(/输入代码或名称/) as HTMLInputElement).value).toBe('');
  });

  it('短于 2 字符不发请求', async () => {
    const spy = vi.fn().mockResolvedValue(new Response('[]', { status: 200 }));
    vi.stubGlobal('fetch', spy);
    renderSearch(vi.fn());
    fireEvent.change(screen.getByPlaceholderText(/输入代码或名称/), { target: { value: '6' } });
    await waitFor(() => expect(spy).not.toHaveBeenCalled());
  });
});
