import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { NewsFeedList, fmtNewsTime } from '../NewsFeedList';
import type { NewsItemDTO } from '../types';

function item(over: Partial<NewsItemDTO> = {}): NewsItemDTO {
  return {
    news_id: 'n1',
    source: 'cls_telegraph',
    source_name: '财联社电报',
    external_id: 'e1',
    title: '央行开展 2000 亿元逆回购',
    content: '  央行公告：为维护流动性合理充裕，\n 开展 2000 亿元逆回购操作。 ',
    url: 'https://example.com/n1',
    industry_code: 'bank',
    symbols: ['600000.SH'],
    published_at: '2026-09-10T10:30:00',
    collected_at: '2026-09-10T10:31:00',
    quality_flags: 0,
    source_tag: 'telegraph',
    ...over,
  };
}

describe('fmtNewsTime', () => {
  it('格式化为 MM-DD HH:mm；缺失/非法 → —', () => {
    expect(fmtNewsTime('2026-09-10T10:30:00')).toBe('09-10 10:30');
    expect(fmtNewsTime(null)).toBe('—');
    expect(fmtNewsTime('not-a-date')).toBe('—');
  });
});

describe('NewsFeedList', () => {
  afterEach(() => vi.restoreAllMocks());

  it('渲染条目：时间/来源名/标题链接/摘要/个股标签', () => {
    render(<NewsFeedList items={[item()]} total={1} limit={50} offset={0} />);
    expect(screen.getByText('09-10 10:30')).toBeInTheDocument();
    expect(screen.getByText('财联社电报')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: '央行开展 2000 亿元逆回购' });
    expect(link).toHaveAttribute('href', 'https://example.com/n1');
    expect(screen.getByText(/为维护流动性合理充裕/)).toBeInTheDocument();
    expect(screen.getByText('600000.SH')).toBeInTheDocument();
  });

  it('空列表 → 空态文案，无加载更多', () => {
    render(<NewsFeedList items={[]} total={0} limit={50} offset={0} />);
    expect(screen.getByText(/暂无资讯/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('每条资讯标题都可点击（url 存在 → 原文链接，target=_blank）', () => {
    const items = [
      item({ news_id: 'a', title: '财联社电报条目', url: 'https://www.cls.cn/detail/1' }),
      item({ news_id: 'b', title: '新浪快讯条目', url: 'https://finance.sina.com.cn/7x24/' }),
    ];
    render(<NewsFeedList items={items} total={2} limit={50} offset={0} />);
    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(2);
    for (const l of links) {
      expect(l).toHaveAttribute('target', '_blank');
      expect(l.getAttribute('href')).toMatch(/^https:/);
    }
  });

  it('offset+已载 < total 时出现加载更多，点击回调', () => {
    const onLoadMore = vi.fn();
    render(
      <NewsFeedList items={[item()]} total={101} limit={50} offset={50} onLoadMore={onLoadMore} />,
    );
    expect(screen.getByText(/已载 51\/101/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button'));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it('已全部载入时不显示加载更多', () => {
    render(
      <NewsFeedList items={[item()]} total={1} limit={50} offset={0} onLoadMore={vi.fn()} />,
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('生产路径数据无 source_tag（空串）时徽章仍按 source 类别着色', () => {
    // 采集器从不写 source_tag → 生产入库恒为 ""，契约字段是 source
    const raw = item({ source: 'telegraph', source_tag: '' });
    render(<NewsFeedList items={[raw]} total={1} limit={50} offset={0} />);
    expect(screen.getByText('电报').className).toContain('bg-emerald-50');
  });
});
