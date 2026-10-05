/**
 * ExpressionPane 的行为契约。
 *
 * 最要紧的一条是**状态诚实**：结论带着它对应的那段文本（`sync.expression`）。
 * 文本改过、新结论还没回来时，界面必须显示「校验中…」而不是继续挂着上一次的
 * 「✓ 引擎可解析」—— 否则界面在替一段从未校验过的 DSL 背书，用户会直接保存。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { SyncState } from '../useFactorEditor';
import ExpressionPane from '../ExpressionPane';

// CodeMirror 要量 DOM，jsdom 下换成 textarea。具名导出也要给：
// 组件顶层就用它们建高亮插件，缺一个模块初始化就会抛。
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange?: (v: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      aria-label="DSL 表达式"
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
  Decoration: { mark: (spec: unknown) => spec },
  MatchDecorator: class {
    createDeco() {
      return null;
    }
    updateDeco() {
      return null;
    }
  },
  ViewPlugin: { fromClass: (cls: unknown, spec: unknown) => ({ cls, spec }) },
  EditorView: {
    theme: (spec: unknown) => spec,
    lineWrapping: 'line-wrapping',
    domEventHandlers: (handlers: unknown) => handlers,
  },
}));

// 与 backtests/__tests__/page.test.tsx 同法：把动态组件真的加载出来
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

const DSL = 'Ts_Mean($close,5)';

function makeSync(overrides: Partial<SyncState> = {}): SyncState {
  return {
    expression: DSL,
    checking: false,
    error: null,
    normalized: DSL,
    translated: false,
    canvasWarnings: [],
    ...overrides,
  };
}

function setup(
  sync: SyncState,
  value = DSL,
  extra: { collapsed?: boolean; onToggle?: () => void; onSave?: () => void } = {},
) {
  const onChange = vi.fn();
  const onFormat = vi.fn();
  const onRevert = vi.fn();

  // 受控组件：自己持有文本，才能像真实页面那样连续输入
  function Harness() {
    const [current, setCurrent] = useState(value);
    return (
      <ExpressionPane
        value={current}
        onChange={(next) => {
          setCurrent(next);
          onChange(next);
        }}
        sync={sync}
        onFormat={onFormat}
        onRevert={onRevert}
        {...extra}
      />
    );
  }

  render(<Harness />);
  return { onChange, onFormat, onRevert };
}

describe('ExpressionPane 状态诚实', () => {
  it('结论属于当前文本：显示可解析', async () => {
    setup(makeSync());
    expect(await screen.findByText('✓ 引擎可解析')).toBeInTheDocument();
    expect(screen.queryByText('校验中…')).toBeNull();
  });

  it('文本已改、结论还是上一段的：显示校验中，不得显示可解析', async () => {
    setup(makeSync({ expression: 'Ts_Mean($close,10)', normalized: 'Ts_Mean($close,10)' }));
    expect(await screen.findByText('校验中…')).toBeInTheDocument();
    expect(screen.queryByText('✓ 引擎可解析')).toBeNull();
  });

  it('服务端报错：原文带出原因', async () => {
    setup(makeSync({ error: '未知算子 Ts_Bogus', normalized: null }));
    expect(await screen.findByText(/未知算子 Ts_Bogus/)).toBeInTheDocument();
    expect(screen.queryByText('✓ 引擎可解析')).toBeNull();
  });

  it('空文本且无结论：不表态', async () => {
    setup(makeSync({ expression: '', normalized: '' }), '');
    expect(await screen.findByLabelText('DSL 表达式')).toBeInTheDocument();
    expect(screen.queryByText('✓ 引擎可解析')).toBeNull();
    expect(screen.queryByText('校验中…')).toBeNull();
  });
});

describe('ExpressionPane 提示与交互', () => {
  it('归一写法与当前文本不同：显示归一提示，点归一回调', async () => {
    const user = userEvent.setup();
    const { onFormat } = setup(
      makeSync({ expression: 'Ts_Mean($close, 5)', normalized: DSL }),
      'Ts_Mean($close, 5)',
    );

    expect(await screen.findByText(/归一为/)).toBeInTheDocument();
    const button = screen.getByRole('button', { name: '归一' });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(onFormat).toHaveBeenCalledTimes(1);
  });

  it('文本已改、结论是上一段的：归一禁用且不显示上一段的归一结果', async () => {
    // 这是数据丢失路径：手里的 normalized 属于上一段文本，点归一会把刚敲的改动抹掉
    setup(makeSync({ expression: 'Ts_Mean($close,10)', normalized: 'Ts_Mean($close,10)' }), DSL);

    await screen.findByLabelText('DSL 表达式');
    expect(screen.getByRole('button', { name: '归一' })).toBeDisabled();
    expect(screen.queryByText(/归一为/)).toBeNull();
  });

  it('已是规范写法：归一按钮禁用', async () => {
    setup(makeSync());
    await screen.findByLabelText('DSL 表达式');
    expect(screen.getByRole('button', { name: '归一' })).toBeDisabled();
  });

  it('有损映射：如实说明画布只能近似表示', async () => {
    setup(makeSync({ canvasWarnings: ['参数 3 是表达式，画布只记了数值'] }));
    expect(await screen.findByText(/画布只能近似表示/)).toBeInTheDocument();
    expect(screen.getByText(/参数 3 是表达式/)).toBeInTheDocument();
  });

  it('历史 qlib 写法：说明已翻译，保存后以 DSL 存储', async () => {
    setup(makeSync({ translated: true }));
    expect(await screen.findByText(/历史 qlib 写法/)).toBeInTheDocument();
  });

  it('折叠：不渲染编辑器，但归一提示与展开开关仍在', async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    setup(makeSync({ expression: 'Ts_Mean($close, 5)', normalized: DSL }), 'Ts_Mean($close, 5)', {
      collapsed: true,
      onToggle,
    });

    expect(screen.queryByLabelText('DSL 表达式')).toBeNull();
    // 折叠后仍要能看到「归一为」——收起只是让出高度，不是把结论也藏了
    expect(await screen.findByText(/归一为/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '▸' }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('输入改动上报给上层', async () => {
    const user = userEvent.setup();
    const { onChange } = setup(makeSync());
    const box = await screen.findByLabelText('DSL 表达式');

    await user.clear(box);
    await user.type(box, 'Rank($close)');
    expect(onChange).toHaveBeenLastCalledWith('Rank($close)');
  });

  it('回退按钮把控制权交回画布', async () => {
    const user = userEvent.setup();
    const { onRevert } = setup(makeSync());
    await screen.findByLabelText('DSL 表达式');

    await user.click(screen.getByRole('button', { name: '回退到画布' }));
    expect(onRevert).toHaveBeenCalledTimes(1);
  });
});
