// Markdown 渲染 —— GFM 表格/列表成真实元素、代码块高亮、原始 HTML 不落 DOM（防 XSS）
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import Markdown from '../Markdown';

describe('Markdown', () => {
  it('渲染标题 / 粗体 / 列表', () => {
    render(<Markdown content={'## 结论\n\n近一年 **上涨 12%**\n\n- 茅台\n- 五粮液'} />);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('结论');
    expect(screen.getByText('上涨 12%').tagName).toBe('STRONG');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('渲染 GFM 表格为真实 table', () => {
    const md = '| 代码 | 涨幅 |\n| --- | --- |\n| 600519 | +12% |';
    const { container } = render(<Markdown content={md} />);
    expect(container.querySelector('table')).not.toBeNull();
    expect(screen.getByText('600519')).toBeInTheDocument();
    expect(screen.getByText('涨幅')).toBeInTheDocument();
  });

  it('代码块带高亮类，行内代码不带', () => {
    const { container } = render(<Markdown content={'```python\nx = 1\n```\n\n行内 `y` 代码'} />);
    expect(container.querySelector('pre code.hljs')).not.toBeNull();
    const inline = screen.getByText('y');
    expect(inline.tagName).toBe('CODE');
    expect(inline.className).not.toContain('hljs');
  });

  it('无语言标记的裸代码块走 pre，不误套行内样式', () => {
    const { container } = render(<Markdown content={'```\nplain output\n```'} />);
    const code = container.querySelector('pre > code');
    expect(code).not.toBeNull();
    // 行内样式已移到 CSS `:not(pre) > code`；元素本身不该带描边/底色类
    expect(code?.className ?? '').toBe('');
  });

  it('链接新开窗口且带 noopener', () => {
    render(<Markdown content={'[东财](https://eastmoney.com)'} />);
    const a = screen.getByRole('link', { name: '东财' });
    expect(a).toHaveAttribute('target', '_blank');
    expect(a.getAttribute('rel')).toContain('noopener');
  });

  it('原始 HTML 不落 DOM（防 XSS）', () => {
    const { container } = render(
      <Markdown content={'<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">'} />,
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });

  it('javascript: 链接被剥离（防 XSS）', () => {
    const { container } = render(<Markdown content={'[点我](javascript:alert(1))'} />);
    // react-markdown 的 defaultUrlTransform 会把不安全协议清空 → 不残留可点的 javascript: href
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.textContent).toContain('点我');
  });
});
