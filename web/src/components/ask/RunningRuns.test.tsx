import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RunsSnapshot } from '@/lib/ask-api';
import { killRun, listRuns } from '@/lib/ask-api';
import { renderPage } from '@/test/page-utils';
import RunningRuns from './RunningRuns';

vi.mock('@/lib/ask-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ask-api')>();
  return { ...actual, listRuns: vi.fn(), killRun: vi.fn() };
});

const mockedList = vi.mocked(listRuns);
const mockedKill = vi.mocked(killRun);

const snapshot: RunsSnapshot = {
  runs: [
    {
      session_id: 's1',
      provider: 'claude_code',
      elapsed_seconds: 8.3,
      pid: 12345,
      workspace: '/tmp/ws1',
    },
    { session_id: 's2', provider: 'mock', elapsed_seconds: 2, pid: null, workspace: '' },
  ],
  max_concurrent_runs: 4,
};

beforeEach(() => {
  mockedList.mockResolvedValue(snapshot);
  mockedKill.mockResolvedValue({ ok: true, killed: true });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

/** 等数据到位后展开弹层（按钮文案本身就带着数量，是数据已到的信号） */
async function openPanel(name: string) {
  fireEvent.click(await screen.findByRole('button', { name }));
}

describe('RunningRuns', () => {
  it('有 2 个在跑 → 按钮显示「● 运行中 2」，展开后逐条给 provider / 已运行 / pid', async () => {
    renderPage(<RunningRuns />);

    await openPanel('● 运行中 2');
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Claude Code');
    expect(items[0]).toHaveTextContent('已运行 8.3s');
    expect(items[0]).toHaveTextContent('pid 12345');
    expect(items[0]).toHaveTextContent('/tmp/ws1');
    // 并发上限也要露出来：被 429 拒掉时用户得知道是撞了闸而不是坏了
    expect(screen.getByText(/上限 4 个/)).toBeInTheDocument();
  });

  it('无子进程（pid: null）显示「无子进程」，不是空白也不是 pid null', async () => {
    mockedList.mockResolvedValue({ runs: [snapshot.runs[1]], max_concurrent_runs: 4 });
    renderPage(<RunningRuns />);

    fireEvent.click(await screen.findByRole('button', { name: '● 运行中 1' }));
    const item = screen.getByRole('listitem');
    expect(item).toHaveTextContent('无子进程');
    expect(item).not.toHaveTextContent('pid null');
  });

  it('点「终止」→ killRun(session_id)，并立刻刷新列表（不等下一轮轮询）', async () => {
    renderPage(<RunningRuns />);

    await openPanel('● 运行中 2');
    fireEvent.click(screen.getAllByRole('button', { name: '终止' })[0]);

    await waitFor(() => expect(mockedKill).toHaveBeenCalledWith('s1'));
    // 刷新：mutate 会再打一次 listRuns，用户不用盯着「终止中…」等 3 秒
    await waitFor(() => expect(mockedList.mock.calls.length).toBeGreaterThan(1));
  });

  it('killRun 抛错 → 错误显示在弹层里；弹层不关，用户能再试一次', async () => {
    mockedKill.mockRejectedValue(new Error('run 已经结束了'));
    renderPage(<RunningRuns />);

    await openPanel('● 运行中 2');
    fireEvent.click(screen.getAllByRole('button', { name: '终止' })[0]);

    expect(await screen.findByText('run 已经结束了')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '运行中的 agent' })).toBeInTheDocument();
  });

  it('空列表 → 按钮「○ 运行中 0」，展开是「当前没有在跑的回答」而不是一个空框', async () => {
    mockedList.mockResolvedValue({ runs: [], max_concurrent_runs: 4 });
    renderPage(<RunningRuns />);

    await openPanel('○ 运行中 0');
    expect(screen.getByText('当前没有在跑的回答')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).toBeNull();
  });

  it('点「打开」把 session_id 交给 onOpen（父级据此切到那条会话）', async () => {
    const onOpen = vi.fn();
    renderPage(<RunningRuns onOpen={onOpen} />);

    await openPanel('● 运行中 2');
    fireEvent.click(screen.getAllByRole('button', { name: '打开' })[1]);

    expect(onOpen).toHaveBeenCalledWith('s2');
    // 打开后弹层收起，避免挡着刚切过去的会话
    expect(screen.queryByRole('dialog', { name: '运行中的 agent' })).toBeNull();
  });
});
