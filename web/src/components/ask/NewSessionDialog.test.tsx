import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AgentCapabilities } from '@/lib/agent-api';
import NewSessionDialog from './NewSessionDialog';

const caps: AgentCapabilities = {
  providers: [
    { id: 'claude_code', label: 'Claude Code', available: true },
    { id: 'codex', label: 'Codex', available: false },
  ],
  skills: [
    { name: 'alpha', description: '甲', tags: [], valid: true },
    { name: 'broken', description: '', tags: [], valid: false },
  ],
  mcp_tools: [
    { name: 'get_quotes', description: '行情' },
    { name: 'get_daily', description: '日线' },
  ],
  // skills=null → 全开（全部勾上）；mcp_tools 只勾一个
  defaults: { provider: 'codex', skills: null, mcp_tools: ['get_quotes'] },
};

function setup(onCreate = vi.fn()) {
  render(<NewSessionDialog caps={caps} onCancel={vi.fn()} onCreate={onCreate} />);
  return onCreate;
}

const box = (name: RegExp) => screen.getByRole('checkbox', { name });

describe('NewSessionDialog', () => {
  it('默认值：provider 取 defaults；skills=null 勾上全部**可选**的；mcp 按 defaults', () => {
    setup();
    expect(screen.getByRole('radio', { name: /Codex/ })).toBeChecked();
    expect(box(/alpha/)).toBeChecked();
    expect(box(/get_quotes/)).toBeChecked();
    expect(box(/get_daily/)).not.toBeChecked();
    // 不合格的 skill 选不了 —— 勾上只会让建会话 400
    expect(box(/broken/)).toBeDisabled();
    expect(box(/broken/)).not.toBeChecked();
  });

  it('提示 CLI 未安装 与 skill 不可用，但不阻止选 provider', () => {
    setup();
    expect(screen.getByText(/本机未检测到该 CLI/)).toBeInTheDocument();
    expect(screen.getByText(/名称或 frontmatter 不合格/)).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Codex/ })).not.toBeDisabled();
  });

  it('提交带上勾选的能力集（不合格的不在其中；全选回写成 null=不裁剪）', () => {
    const onCreate = setup();
    fireEvent.click(screen.getByRole('radio', { name: /Claude Code/ }));
    fireEvent.click(box(/get_daily/)); // 两个工具都勾上 → 全选
    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(onCreate).toHaveBeenCalledWith({
      provider: 'claude_code',
      // 全选 = null（不裁剪）：以后新增的 skill / 工具自动带上
      skills: null,
      mcp_tools: null,
    });
  });

  it('只勾部分能力时落显式名单（不是 null）', () => {
    const onCreate = setup();
    fireEvent.click(box(/alpha/)); // 取消唯一的可用 skill
    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(onCreate).toHaveBeenCalledWith({
      provider: 'codex', skills: [], mcp_tools: ['get_quotes'],
    });
  });

  it('全选只选可用的 skill（不合格的不参与，故仍是全选=null）', () => {
    const onCreate = setup();
    fireEvent.click(screen.getAllByRole('button', { name: '全选' })[0]);
    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(onCreate.mock.calls[0][0].skills).toBeNull();
  });

  it('清空 → 提交空列表（一个都不启用，与「不传」语义不同）', () => {
    const onCreate = setup();
    const clears = screen.getAllByRole('button', { name: '清空' });
    fireEvent.click(clears[0]); // 第一组 = Skill
    fireEvent.click(clears[1]); // 第二组 = MCP 工具
    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(onCreate).toHaveBeenCalledWith({
      provider: 'codex', skills: [], mcp_tools: [],
    });
  });

  it('全选恢复全部（回写成不裁剪）', () => {
    const onCreate = setup();
    fireEvent.click(screen.getAllByRole('button', { name: '全选' })[1]);
    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(onCreate.mock.calls[0][0].mcp_tools).toBeNull();
  });

  it('busy 时禁用提交并显示进度文案', () => {
    const onCreate = vi.fn();
    render(
      <NewSessionDialog caps={caps} busy onCreate={onCreate} onCancel={vi.fn()} />,
    );
    const submit = screen.getByRole('button', { name: '创建中…' });
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('错误信息展示在页脚', () => {
    render(
      <NewSessionDialog caps={caps} error="未知 provider: gpt" onCreate={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(screen.getByText('未知 provider: gpt')).toBeInTheDocument();
  });

  it('取消走 onCancel', () => {
    const onCancel = vi.fn();
    render(<NewSessionDialog caps={caps} onCreate={vi.fn()} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onCancel).toHaveBeenCalled();
  });

  it('清单为空时给出空态而不是空白块', () => {
    render(
      <NewSessionDialog
        caps={{ providers: [], skills: [], mcp_tools: [], defaults: {} }}
        onCreate={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getAllByText('暂无可选项')).toHaveLength(2);
    // 没有可选 provider 时不能提交（provider 为空）
    expect(screen.getByRole('button', { name: '创建会话' })).toBeDisabled();
  });
});
