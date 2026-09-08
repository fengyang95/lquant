import '@testing-library/jest-dom/vitest';

// RTL 自动 cleanup 依赖 vitest globals；未开 globals 时需手动注册，
// 否则多用例渲染累积会出现 "Found multiple elements"。
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});

// next/link 在 jsdom 下可用，但为保险起见 mock 掉 router 依赖

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});
