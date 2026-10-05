// 运行中反馈 + 取消 + 丢弃保护 —— page 级集成测试。
//
// 这三件事此前都缺：回测要跑 26~39s，用户只看到一句「执行中…」，
// 没有阶段、没有已用时、不能取消；载入/新建/切 Tab 还会直接吞掉未保存的代码。
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { useEffect, useState } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Page from '../page';

vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  fetcher: vi.fn(),
  post: vi.fn(),
  postData: vi.fn(),
  putData: vi.fn(),
  del: vi.fn(),
}));

const swrState: { strategies: unknown[] } = { strategies: [] };
vi.mock('swr', () => ({
  default: (key: unknown) => ({
    data: key === '/strategies' ? swrState.strategies : undefined,
    mutate: vi.fn(),
  }),
  mutate: vi.fn(),
}));

const params = new URLSearchParams();
vi.mock('next/navigation', () => ({
  useSearchParams: () => params,
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock('@uiw/react-codemirror', () => {
  const Fake = ({
    value,
    onChange,
  }: {
    value: string;
    onChange?: (v: string) => void;
  }) => (
    <textarea aria-label="编辑器" value={value} onChange={(e) => onChange?.(e.target.value)} />
  );
  return { default: Fake };
});
vi.mock('next/dynamic', () => ({
  default: (loader: () => Promise<unknown>) => {
    const Comp = (props: Record<string, unknown>) => {
      const [Real, setReal] = useState<{
        default: React.ComponentType<Record<string, unknown>>;
      } | null>(null);
      useEffect(() => {
        void loader().then((m) =>
          setReal(m as { default: React.ComponentType<Record<string, unknown>> }),
        );
      }, [loader]);
      if (!Real) return null;
      const C = Real.default;
      return <C {...props} />;
    };
    return Comp;
  },
}));

vi.mock('../workspace/HistoryPanel', () => ({
  __esModule: true,
  default: () => <div>回测记录</div>,
}));

import { get, post } from '@/lib/api';
const getMock = vi.mocked(get);
const postMock = vi.mocked(post);

beforeEach(() => {
  vi.clearAllMocks();
  params.delete('id');
  params.delete('run');
  swrState.strategies = [];
  getMock.mockResolvedValue(undefined);
});

/**
 * 推进轮询：handleRun 用 setTimeout 退避等待（真实等待 2s 起步），测试里用假
 * 时钟快进。每次 advanceTimersByTimeAsync 会连续跑完多个轮询周期（退避后
 * 20s 内可能包含数次），所以断言文案时要用"包含阶段名"而非精确等于某一帧。
 */
async function advancePolling(steps = 1) {
  for (let i = 0; i < steps; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
  }
}

function setupFakeTimers() {
  vi.useFakeTimers({ shouldAdvanceTime: true });
}

afterEach(() => {
  vi.useRealTimers();
});

/** 让 get('/backtests/run-code/{job}') 依次返回给定状态序列。 */
function stubStatusSequence(seq: Array<Record<string, unknown>>) {
  let i = 0;
  getMock.mockImplementation((path: string) => {
    if (path.startsWith('/backtests/run-code/')) {
      const st = seq[Math.min(i, seq.length - 1)];
      i += 1;
      return Promise.resolve(st);
    }
    return Promise.resolve(undefined);
  });
}

describe('运行中反馈与取消', () => {
  it('运行中展示后端阶段名与已用时长（不再是笼统的「执行中」）', async () => {
    setupFakeTimers();
    postMock.mockResolvedValue({ job_id: 'j1' } as never);
    // 固定同一阶段，避免快进多轮后阶段推进导致断言落在别的帧上
    stubStatusSequence([{ status: 'running', progress: { phase: '运行策略' } }]);
    render(<Page />);
    await userEvent.click(await screen.findByRole('button', { name: /编译运行/ }));
    await advancePolling(1);

    // 文案形如「运行策略… 2s」：阶段名来自后端，时长来自前端计时
    expect(screen.getByText(/运行策略…\s*\d/)).toBeInTheDocument();
  });

  it('运行中右栏出现「取消运行」，点击调用取消端点', async () => {
    postMock.mockResolvedValue({ job_id: 'j1' } as never);
    stubStatusSequence([{ status: 'running', progress: { phase: '运行策略' } }]);
    render(<Page />);
    await userEvent.click(await screen.findByRole('button', { name: /编译运行/ }));

    const cancelBtn = await screen.findByRole('button', { name: '取消运行' });
    await userEvent.click(cancelBtn);

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith('/backtests/run-code/j1/cancel', {}),
    );
  });

  it('取消返回 409（任务刚好已结束）不当作错误弹给用户', async () => {
    postMock.mockImplementation((path: string) => {
      if (path.endsWith('/cancel')) return Promise.reject(new Error('409 /backtests/run-code/j1/cancel'));
      return Promise.resolve({ job_id: 'j1' } as never);
    });
    stubStatusSequence([{ status: 'running', progress: { phase: '运行策略' } }]);
    render(<Page />);
    await userEvent.click(await screen.findByRole('button', { name: /编译运行/ }));
    await userEvent.click(await screen.findByRole('button', { name: '取消运行' }));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(screen.queryByText(/409/)).not.toBeInTheDocument();
  });

  it('任务完成后再无「取消运行」入口', async () => {
    setupFakeTimers();
    postMock.mockResolvedValue({ job_id: 'j1' } as never);
    stubStatusSequence([
      { status: 'running', progress: { phase: '运行策略' } },
      { status: 'done', run_id: 'r1' },
    ]);
    render(<Page />);
    await userEvent.click(await screen.findByRole('button', { name: /编译运行/ }));
    await advancePolling(2);

    expect(screen.queryByRole('button', { name: '取消运行' })).not.toBeInTheDocument();
  });

  it('后端返回 failed 时把错误信息透出到页面', async () => {
    setupFakeTimers();
    postMock.mockResolvedValue({ job_id: 'j1' } as never);
    stubStatusSequence([{ status: 'failed', error: '策略未产生净值（检查数据区间与标的代码）' }]);
    render(<Page />);
    await userEvent.click(await screen.findByRole('button', { name: /编译运行/ }));
    await advancePolling(2);

    expect(screen.getByText(/策略未产生净值/)).toBeInTheDocument();
  });
});

describe('未保存改动的丢弃保护', () => {
  it('有未保存改动时点「新建」→ 先确认；用户取消则代码保留', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<Page />);
    // 制造 dirty：写入编辑器
    const editor = await screen.findByLabelText('编辑器');
    await userEvent.clear(editor);
    await userEvent.type(editor, 'print(1)');

    await userEvent.click(screen.getByRole('button', { name: '新建' }));

    expect(confirmSpy).toHaveBeenCalled();
    // 用户拒绝 → 内容还在
    expect(screen.getByLabelText('编辑器')).toHaveValue('print(1)');
    confirmSpy.mockRestore();
  });

  it('用户确认丢弃 → 编辑器复位为默认模板', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<Page />);
    const editor = await screen.findByLabelText('编辑器');
    await userEvent.clear(editor);
    await userEvent.type(editor, 'print(1)');

    await userEvent.click(screen.getByRole('button', { name: '新建' }));

    expect(screen.getByLabelText('编辑器')).not.toHaveValue('print(1)');
    expect(screen.getByLabelText('开始日期')).toHaveValue('2024-01-01');
    confirmSpy.mockRestore();
  });

  it('没有改动时点「新建」不打扰用户（不弹确认）', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<Page />);
    await screen.findByLabelText('编辑器');

    await userEvent.click(screen.getByRole('button', { name: '新建' }));

    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('有未保存改动时切 Tab → 先确认；取消则停留在当前页签', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<Page />);
    const editor = await screen.findByLabelText('编辑器');
    await userEvent.clear(editor);
    await userEvent.type(editor, 'print(1)');

    await userEvent.click(screen.getByRole('button', { name: '快速回测' }));

    expect(confirmSpy).toHaveBeenCalled();
    // 未被切走：编辑器仍在
    expect(screen.getByLabelText('编辑器')).toBeInTheDocument();
    confirmSpy.mockRestore();
  });
});
