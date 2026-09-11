import { describe, expect, it, vi, beforeEach } from 'vitest';
import { useEffect, useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EditorPane from '../EditorPane';
import type { Snapshot } from '../state';

// CodeMirror 依赖浏览器 DOM API，jsdom 下无法渲染/交互 —— 用 stub 代替：
// 展示 code 文本，并提供一个「编辑器」textarea 触发 onChange。
vi.mock('@uiw/react-codemirror', () => {
  const Fake = ({
    value,
    onChange,
  }: {
    value: string;
    onChange?: (v: string) => void;
  }) => (
    <textarea
      aria-label="编辑器"
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  return { default: Fake };
});

// dynamic(ssr:false) 首帧渲染 loading 态，测试中改为同步加载
vi.mock('next/dynamic', () => ({
  default: (loader: () => Promise<unknown>) => {
    const Comp = (props: Record<string, unknown>) => {
      const [Real, setReal] = useState<{ default: React.ComponentType<Record<string, unknown>> } | null>(
        null,
      );
      useEffect(() => {
        void loader().then((m) => setReal(m as { default: React.ComponentType<Record<string, unknown>> }));
      }, [loader]);
      if (!Real) return null;
      const C = Real.default;
      return <C {...props} />;
    };
    return Comp;
  },
}));

const BASE: Snapshot = {
  name: '双均线',
  description: '示例描述',
  code: 'print(1)',
  params: { start: '2024-01-01', end: '2024-12-31', formulas: 'pct_change_20' },
};

function setup(selectedId: string | null, onChange = vi.fn()) {
  render(
    <EditorPane
      name={BASE.name}
      description={BASE.description}
      code={BASE.code}
      params={BASE.params}
      selectedId={selectedId}
      onChange={onChange}
    />,
  );
  return onChange;
}

beforeEach(() => {
  // dynamic(ssr:false) 在测试环境同步加载 mock 模块，忽略加载态
  vi.stubGlobal('IntersectionObserver', undefined);
});

describe('EditorPane', () => {
  it('渲染名称/描述/开始/结束/因子公式五个字段与编辑器', async () => {
    setup(null);
    expect(screen.getByLabelText('策略名称')).toBeInTheDocument();
    expect(screen.getByLabelText('描述')).toBeInTheDocument();
    expect(screen.getByLabelText('开始日期')).toBeInTheDocument();
    expect(screen.getByLabelText('结束日期')).toBeInTheDocument();
    expect(screen.getByLabelText('因子公式')).toBeInTheDocument();
    expect(await screen.findByLabelText('编辑器')).toHaveValue(BASE.code);
  });

  it("selectedId='a' 时名称为只读 div；selectedId=null 时为可编辑 input", async () => {
    const onChange = setup('a');
    const ro = screen.getByText('双均线');
    expect(ro).toHaveAttribute('title', '改名需另存为新策略');
    expect(screen.queryByLabelText('策略名称')).not.toBeInTheDocument();

    // 其他字段仍可编辑并上报快照
    await userEvent.type(screen.getByLabelText('描述'), '!');
    expect(onChange).toHaveBeenCalled();

    // 新建模式：名称恢复为可编辑 input（受控组件，变化经 onChange 上报）
    const onChange2 = setup(null);
    const input = screen.getByLabelText('策略名称') as HTMLInputElement;
    expect(input.value).toBe(BASE.name);
    await userEvent.type(input, 'x');
    expect(onChange2).toHaveBeenCalled();
  });

  it('修改描述字段触发全量快照 onChange', async () => {
    const onChange = setup(null);
    await userEvent.type(screen.getByLabelText('描述'), '!');
    const calls = onChange.mock.calls as unknown as Snapshot[][];
    expect(calls.length).toBeGreaterThan(0);
    const last = calls[calls.length - 1][0];
    expect(last).toEqual({
      name: BASE.name,
      description: `${BASE.description}!`,
      code: BASE.code,
      params: BASE.params,
    });
  });
});
