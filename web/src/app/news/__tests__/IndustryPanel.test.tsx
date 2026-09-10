import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { IndustryPanel } from '../IndustryPanel';

function envelope(data: unknown) {
  return new Response(JSON.stringify({ code: 0, data, message: 'ok' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function item(newsId: string) {
  return {
    news_id: newsId,
    source: 'cls_telegraph',
    source_name: '财联社电报',
    external_id: newsId,
    title: `${newsId} 标题`,
    content: '内容',
    url: null,
    industry_code: 'bank',
    symbols: [],
    published_at: '2026-09-10T09:00:00',
    collected_at: null,
    quality_flags: 0,
    source_tag: 'telegraph',
  };
}

const INDUSTRIES = [
  { industry_code: 'bank', count: 12, industry_name: '银行' },
  { industry_code: 'semiconductor', count: 3, industry_name: '半导体' },
];

function renderPanel() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <IndustryPanel />
    </SWRConfig>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('IndustryPanel', () => {
  it('渲染行业列表（中文名 + 计数），点击行业 → fetchItems({industry: code}) 并展示条目', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        calls.push(url);
        if (url.includes('/news/industries')) return envelope(INDUSTRIES);
        if (url.includes('/news/items')) {
          return envelope({ total: 1, items: [item('n1')] });
        }
        return new Response('not found', { status: 404 });
      }),
    );
    renderPanel();

    // 左列出现行业
    expect(await screen.findByText('银行')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();

    // 点击行业 → 右侧拉取该行业条目
    fireEvent.click(screen.getByRole('button', { name: /银行/ }));
    await waitFor(() => {
      expect(screen.getByText('n1 标题')).toBeInTheDocument();
    });
    const itemCall = calls.find((u) => u.includes('/news/items'));
    expect(itemCall).toContain('industry=bank');
  });

  it('行业列表为空 → 空态', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) =>
        url.includes('/news/industries') ? envelope([]) : new Response('not found', { status: 404 }),
      ),
    );
    renderPanel();
    expect(await screen.findByText('暂无行业数据')).toBeInTheDocument();
  });
});
