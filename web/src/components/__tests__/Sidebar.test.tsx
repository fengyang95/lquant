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
});
