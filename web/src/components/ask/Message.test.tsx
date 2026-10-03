// 单条消息：助手回答走 Markdown（表格/粗体成元素），用户输入保持纯文本
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AskMessage } from '@/lib/ask-api';

import Message from './Message';

function msg(partial: Partial<AskMessage>): AskMessage {
  return {
    id: 'm1',
    session_id: 's1',
    role: 'assistant',
    content: '',
    tool_calls: [],
    created_at: '2026-10-03T00:00:00Z',
    ...partial,
  };
}

describe('Message', () => {
  it('助手回答按 Markdown 渲染', () => {
    render(<Message message={msg({ role: 'assistant', content: '**结论**：上涨' })} />);
    expect(screen.getByText('结论').tagName).toBe('STRONG');
  });

  it('用户输入保持纯文本，不当 Markdown 解析', () => {
    render(<Message message={msg({ role: 'user', content: '**这不是粗体**' })} />);
    // 原样展示，不产生 <strong>
    expect(screen.getByText('**这不是粗体**')).toBeInTheDocument();
    expect(screen.queryByText('这不是粗体')).toBeNull();
  });
});
