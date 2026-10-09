import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import Sidebar from '../Sidebar';

// jsdom 无 app router 上下文
vi.mock('next/navigation', () => ({ usePathname: () => '/dashboard' }));

describe('Sidebar', () => {
  it('包含 /news 资讯入口', () => {
    render(<Sidebar />);
    const link = screen.getByRole('link', { name: '资讯' });
    expect(link).toHaveAttribute('href', '/news');
  });

  it('不再包含「策略编辑」入口', () => {
    render(<Sidebar />);
    expect(screen.queryByText('策略编辑')).not.toBeInTheDocument();
  });

  it('个股分析与行业分析成对出现在「市场」组', () => {
    render(<Sidebar />);
    expect(screen.getByRole('link', { name: '个股分析' }))
      .toHaveAttribute('href', '/security');
    expect(screen.getByRole('link', { name: '行业分析' }))
      .toHaveAttribute('href', '/industry');
  });
});
