import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SourceBadge } from '../SourceBadge';

describe('SourceBadge 四色映射', () => {
  const CASES: [string, string, string][] = [
    // [kind, 文案, class 关键片段]
    ['telegraph', '电报', 'bg-emerald-50'],
    ['news', '新闻', 'bg-blue-50'],
    ['social', '社媒', 'bg-purple-50'],
    ['report', '研报', 'bg-orange-50'],
  ];

  it.each(CASES)('%s → %s', (kind, text, cls) => {
    render(<SourceBadge kind={kind} />);
    expect(screen.getByText(text).className).toContain(cls);
  });

  it('未知/空 kind 退灰且原样透出（后端新增枚举不空白）', () => {
    render(<SourceBadge kind="weibo_x" />);
    const el = screen.getByText('weibo_x');
    expect(el.className).toContain('bg-neutral-100');
    render(<SourceBadge kind={null} />);
    expect(screen.getByText('未知').className).toContain('bg-neutral-100');
  });
});
