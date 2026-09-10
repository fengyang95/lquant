import { describe, expect, it, vi, beforeEach } from 'vitest';
import { useEffect, useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Page from '../page';

// ---- api mock ----
vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  postData: vi.fn(),
  putData: vi.fn(),
  del: vi.fn(),
}));

// ---- swr mock：/strategies 返回可配置列表，其余（ResultPane/HistoryPanel）为空 ----
const swrState: { strategies: unknown[] } = { strategies: [] };
vi.mock('swr', () => ({
  default: (key: unknown) => ({
    data: key === '/strategies' ? swrState.strategies : undefined,
    mutate: vi.fn(),
  }),
  mutate: vi.fn(),
}));

// ---- next/navigation：useSearchParams 可配置 stub ----
const params = new URLSearchParams();
vi.mock('next/navigation', () => ({
  useSearchParams: () => params,
  useRouter: () => ({ push: vi.fn() }),
}));

// ---- CodeMirror stub（与 EditorPane.test.tsx 同法）----
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

// ---- HistoryPanel stub：提供「载入」按钮，把 row 交给 onLoadRun 验证路由逻辑 ----
vi.mock('../workspace/HistoryPanel', () => ({
  __esModule: true,
  default: ({ onLoadRun }: { onLoadRun: (row: unknown) => void }) => (
    <>
      <div>回测记录</div>
      <button
        onClick={() =>
          onLoadRun({
            run_id: 'r9',
            strategy: 'factor_rotation',
            params: { strategy_id: 's9' },
          })
        }
      >
        载入
      </button>
    </>
  ),
}));

import { del, get } from '@/lib/api';
const getMock = vi.mocked(get);
const delMock = vi.mocked(del);

function setUserStrategies(list: unknown[]) {
  swrState.strategies = list;
}

beforeEach(() => {
  vi.clearAllMocks();
  params.delete('id');
  params.delete('run');
  setUserStrategies([]);
});

describe('回测工作台 page', () => {
  it('默认 Tab 渲染三栏（策略库 / 编辑器 / 结果区）', async () => {
    setUserStrategies([{ id: 's1', name: '甲策略', source: 'user' }]);
    render(<Page />);
    // RunBar 在页头
    expect(await screen.findByRole('button', { name: /编译运行/ })).toBeInTheDocument();
    // 左栏策略库出现策略
    expect(await screen.findByText('甲策略')).toBeInTheDocument();
    // 中栏编辑器 + 右栏空态
    expect(screen.getByLabelText('编辑器')).toBeInTheDocument();
    expect(screen.getByText(/开始第一次回测/)).toBeInTheDocument();
    // 三个 Tab 按钮存在
    expect(screen.getByRole('button', { name: '策略回测' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '快速回测' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '历史与对比' })).toBeInTheDocument();
  });

  it('切换到「快速回测」Tab 渲染 QuickRunPanel', async () => {
    render(<Page />);
    await userEvent.click(screen.getByRole('button', { name: '快速回测' }));
    const panels = await screen.findAllByText('运行回测');
    expect(panels.length).toBeGreaterThan(0);
  });

  it('切换到「历史与对比」Tab 渲染 HistoryPanel', async () => {
    render(<Page />);
    await userEvent.click(screen.getByRole('button', { name: '历史与对比' }));
    expect(await screen.findByText('回测记录')).toBeInTheDocument();
  });

  it('历史「载入」row.params.strategy_id 存在 → 走 /strategies/{id} 而非 /code', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/strategies/s9') {
        return Promise.resolve({ id: 's9', name: '乙策略', source: 'print(9)', config: {} });
      }
      return Promise.resolve(undefined);
    });
    render(<Page />);
    await userEvent.click(screen.getByRole('button', { name: '历史与对比' }));
    await userEvent.click(await screen.findByRole('button', { name: '载入' }));
    expect(getMock).toHaveBeenCalledWith('/strategies/s9');
    expect(getMock).not.toHaveBeenCalledWith('/backtests/r9/code');
  });

  it('?run= 参数触发拉取该 run 的代码回填编辑器', async () => {
    params.set('run', 'r123');
    getMock.mockResolvedValue({ run_id: 'r123', code: 'print(7)', benchmark: null, engine: null });
    render(<Page />);
    expect(await screen.findByLabelText('编辑器')).toHaveValue('print(7)');
    expect(getMock).toHaveBeenCalledWith('/backtests/r123/code');
  });

  it('删除当前选中策略 → del 被调且编辑器复位为新建态', async () => {
    setUserStrategies([{ id: 's1', name: '甲策略', source: 'user' }]);
    getMock.mockImplementation((path: string) => {
      if (path === '/strategies/s1') {
        return Promise.resolve({
          id: 's1',
          name: '甲策略',
          source: 'print(1)',
          config: {},
        });
      }
      return Promise.resolve(undefined);
    });
    delMock.mockResolvedValue(undefined);
    render(<Page />);

    // 载入策略 → 已选中态（名称为只读展示，无名称输入框）
    await userEvent.click(await screen.findByRole('button', { name: /甲策略/ }));
    expect(getMock).toHaveBeenCalledWith('/strategies/s1');
    expect(screen.queryByLabelText('策略名称')).not.toBeInTheDocument();

    // 删除 → 确认
    await userEvent.click(screen.getByRole('button', { name: '删除' }));
    await userEvent.click(screen.getByRole('button', { name: '确定' }));
    expect(delMock).toHaveBeenCalledWith('/strategies/s1');
    // 复位新建态：名称输入框重新出现
    expect(await screen.findByLabelText('策略名称')).toBeInTheDocument();
  });
});
