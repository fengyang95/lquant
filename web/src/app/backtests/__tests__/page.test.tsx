import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { useEffect, useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Page from '../page';

// ---- api mock ----
vi.mock('@/lib/api', () => ({
  get: vi.fn(),
  fetcher: vi.fn(),
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

// 「新建」必须回到真正的白纸 —— 只清 name/description/code 会让上一个策略的
// 回测区间与因子留在编辑器里，用户随后「编译运行」跑的是别人的参数；
// 而已处于新建态时再点，若每个 setState 都是原值，React 会 bail out，
// DOM 零变化，按钮看起来完全失灵。
describe('回测工作台「新建」复位', () => {
  // 这些用例先把编辑器改脏再点「新建」。加了丢弃保护后，dirty 时会先弹
  // 确认框（jsdom 的 window.confirm 默认返回 false → 新建会被拦下）。
  // 这里显式同意丢弃，聚焦「复位是否彻底」这个断言目标。
  beforeEach(() => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function editFieldsThenNew() {
    render(<Page />);
    const name = await screen.findByLabelText('策略名称');
    await userEvent.type(name, '待丢弃策略');
    await userEvent.clear(screen.getByLabelText('开始日期'));
    await userEvent.type(screen.getByLabelText('开始日期'), '2019-09-09');
    await userEvent.clear(screen.getByLabelText('因子公式'));
    await userEvent.type(screen.getByLabelText('因子公式'), 'pct_change_5');
    // 顶部 RunBar 的「新建」（精确匹配，避开「+ 新建策略」）
    await userEvent.click(screen.getByRole('button', { name: '新建' }));
  }

  it('点击「新建」清空名称', async () => {
    await editFieldsThenNew();
    expect(screen.getByLabelText('策略名称')).toHaveValue('');
  });

  it('点击「新建」把开始日期复位为默认值，而非沿用上一个策略', async () => {
    await editFieldsThenNew();
    expect(screen.getByLabelText('开始日期')).toHaveValue('2024-01-01');
  });

  it('点击「新建」把因子公式复位为默认值，而非沿用上一个策略', async () => {
    await editFieldsThenNew();
    expect(screen.getByLabelText('因子公式')).toHaveValue('pct_change_20');
  });

  it('点击「新建」清空右栏结果区（回到空态引导）', async () => {
    await editFieldsThenNew();
    expect(screen.getByText(/开始第一次回测/)).toBeInTheDocument();
  });
});

// 载入策略时 base 快照必须完全来自策略自身数据：此前 start/end 取自组件闭包里
// 当前编辑器的旧 params，切到另一个策略会一载入就误报「●未保存」。
describe('回测工作台载入策略的 dirty 基准', () => {
  it('载入 config 带 start/end 的策略 → 回填该日期且不误报未保存', async () => {
    // 载入前编辑器已被改脏，载入保护会先确认；这里同意丢弃以聚焦基准断言
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    setUserStrategies([{ id: 's1', name: '甲策略', source: 'user' }]);
    getMock.mockImplementation((path: string) => {
      if (path === '/strategies/s1') {
        return Promise.resolve({
          id: 's1',
          name: '甲策略',
          source: 'print(1)',
          config: { start: '2023-01-03', end: '2023-06-30', factor_formulas: ['momentum_20'] },
        });
      }
      return Promise.resolve(undefined);
    });
    render(<Page />);

    // 先把编辑器里的日期/因子改成与目标策略不同的值，制造"旧 params 污染"条件
    await userEvent.clear(await screen.findByLabelText('开始日期'));
    await userEvent.type(screen.getByLabelText('开始日期'), '2019-09-09');
    await userEvent.clear(screen.getByLabelText('因子公式'));
    await userEvent.type(screen.getByLabelText('因子公式'), 'pct_change_5');

    await userEvent.click(screen.getByRole('button', { name: /甲策略/ }));

    // 日期与因子来自策略 config
    expect(await screen.findByLabelText('开始日期')).toHaveValue('2023-01-03');
    expect(screen.getByLabelText('结束日期')).toHaveValue('2023-06-30');
    expect(screen.getByLabelText('因子公式')).toHaveValue('momentum_20');
    // 刚载入 = 与 base 一致，不该显示未保存
    expect(screen.queryByText('●未保存')).not.toBeInTheDocument();
  });
});
