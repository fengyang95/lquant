/**
 * useFactorEditor.loadExpression 回归：
 * 打开历史 qlib 写法的因子时，基线要用**服务端归一后**的 DSL，
 * 并把「发生过翻译」如实带回给页面 —— 否则打开即脏、用户也不知道
 * 自己看的是翻译后的表达式。
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();
const postMock = vi.fn();

vi.mock('@/lib/api', () => ({
  get: (...args: unknown[]) => getMock(...args),
  post: (...args: unknown[]) => postMock(...args),
}));

import { useFactorEditor } from '../useFactorEditor';

const CATALOG = {
  ops: [
    { name: 'Ts_Slope', category: 'TS', label: '时序回归斜率', min_window: 1,
      series_arity: 1, params: [{ name: 'n', type: 'window', required: true, default: null }] },
    { name: 'Ts_Mean', category: 'TS', label: '时序均值', min_window: 1,
      series_arity: 1, params: [{ name: 'n', type: 'window', required: true, default: null }] },
  ],
  infix: [
    { token: '+', label: '加法', arity: 2 },
    { token: '-', label: '减法', arity: 2 },
    { token: '*', label: '乘法', arity: 2 },
    { token: '/', label: '除法', arity: 2 },
  ],
  fields: [{ name: 'close', label: '收盘价' }],
};

const LEGACY = 'Slope($close,10)/$close';
const DSL = 'Ts_Slope($close,10)/$close';
const AST = {
  kind: 'binary',
  op: '/',
  left: { kind: 'call', name: 'Ts_Slope', args: [{ kind: 'field', name: 'close' }, { kind: 'number', value: 10 }] },
  right: { kind: 'field', name: 'close' },
};

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((url: string) => {
    if (url === '/factors/ops') return Promise.resolve({ ops: CATALOG.ops, infix: CATALOG.infix });
    if (url === '/factors/fields') return Promise.resolve(CATALOG.fields);
    return Promise.resolve([]);
  });
  postMock.mockImplementation((url: string) => {
    if (url === '/factors/ast') return Promise.resolve({ expression: DSL, ast: AST, translated: true });
    if (url === '/factors/validate') return Promise.resolve({ ok: true, error: null });
    return Promise.resolve({});
  });
});

type Api = ReturnType<typeof useFactorEditor>;
type LoadResult = Awaited<ReturnType<Api['loadExpression']>>;

/** 等目录加载完，否则 decompile 拿不到算子定义 */
async function ready(result: { current: Api }) {
  await waitFor(() => expect(result.current.catalog.ops.length).toBeGreaterThan(0));
}

async function load(result: { current: Api }, expr: string): Promise<LoadResult> {
  let out: LoadResult | undefined;
  await act(async () => {
    out = await result.current.loadExpression(expr);
  });
  return out as LoadResult;
}

describe('useFactorEditor.loadExpression', () => {
  it('历史 qlib 写法：基线用服务端归一后的 DSL，并回传 translated', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    const out = await load(result, LEGACY);

    expect(out).toEqual({ warnings: [], translated: true });
    expect(postMock).toHaveBeenCalledWith('/factors/ast', { expression: LEGACY });
    // 基线是归一后的 DSL，不是原始 qlib 串（否则打开即脏）
    expect(result.current.state.baselineExpression).toBe(DSL);
    // 画布能从 AST 重编译出 DSL
    await waitFor(() =>
      expect(result.current.compiled.expression).toBe('(Ts_Slope($close,10) / $close)'));
    expect(result.current.compiled.warnings).toEqual([]);
  });

  it('已是 DSL：translated=false，基线原样', async () => {
    postMock.mockImplementation((url: string) => {
      if (url === '/factors/ast') return Promise.resolve({ expression: DSL, ast: AST, translated: false });
      if (url === '/factors/validate') return Promise.resolve({ ok: true, error: null });
      return Promise.resolve({});
    });
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    const out = await load(result, DSL);

    expect(out).toEqual({ warnings: [], translated: false });
    expect(result.current.state.baselineExpression).toBe(DSL);
  });

  it('空表达式：重置画布且不请求 /ast', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    const out = await load(result, '   ');
    expect(out).toEqual({ warnings: [], translated: false });
    expect(postMock).not.toHaveBeenCalledWith('/factors/ast', expect.anything());
    expect(result.current.state.baselineExpression).toBe('');
  });
});

/**
 * 文本 ⇄ 画布 双向同步。
 *
 * 这组用例盯的是**静默错误**：编辑器有两个可写入口，一旦收敛规则错了，
 * 用户看到的和保存的就不是一回事，而且界面上不会有任何异常迹象。
 */
describe('useFactorEditor 双向同步', () => {
  const flush = (ms = 600) =>
    act(async () => {
      await new Promise((resolve) => setTimeout(resolve, ms));
    });

  it('改文本：防抖后请求 /ast，并把解析出的树铺到画布', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    act(() => result.current.setText(DSL));

    await waitFor(() => expect(result.current.sync.expression).toBe(DSL), { timeout: 2000 });
    expect(postMock).toHaveBeenCalledWith('/factors/ast', { expression: DSL });
    // 画布跟着文本走
    await waitFor(() =>
      expect(result.current.compiled.expression).toBe('(Ts_Slope($close,10) / $close)'));
    expect(result.current.sync.error).toBeNull();
    expect(result.current.sync.canvasWarnings).toEqual([]);
    expect(result.current.canSave).toBe(true);
  });

  it('改完文本、校验未回：上一次的结论不得放行新表达式', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    act(() => result.current.setText(DSL));
    await waitFor(() => expect(result.current.canSave).toBe(true), { timeout: 2000 });

    // 再敲一个字符：结论立刻失效（sync.expression 还是上一段文本）
    act(() => result.current.setText(`${DSL}+1`));
    expect(result.current.sync.expression).toBe(DSL);
    expect(result.current.canSave).toBe(false);

    // 校验回来后恢复可保存，且保存的是新表达式
    await waitFor(() => expect(result.current.sync.expression).toBe(`${DSL}+1`), { timeout: 2000 });
    expect(result.current.canSave).toBe(true);
  });

  it('解析失败：带出服务端原因、无可保存表达式', async () => {
    postMock.mockImplementation((url: string) => {
      if (url === '/factors/ast') return Promise.reject(new Error('未知算子 Ts_Bogus'));
      return Promise.resolve({});
    });
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);

    act(() => result.current.setText('Ts_Bogus($close,5)'));

    await waitFor(() => expect(result.current.sync.error).toBe('未知算子 Ts_Bogus'), {
      timeout: 2000,
    });
    expect(result.current.canSave).toBe(false);
    expect(result.current.sync.normalized).toBeNull();
    expect(result.current.saveExpression).toBe('');
  });

  it('改画布：文本跟随编译结果，且结论不反过来重铺画布', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);
    await load(result, DSL);

    // 用户在画布上放一个字段并接到因子输出
    act(() => result.current.addBlock('field', 'close'));
    const nodes = result.current.state.nodes;
    const added = nodes[nodes.length - 1];
    const output = nodes.find((node) => node.kind === 'output') as { id: string };
    act(() =>
      result.current.connect({
        id: `${added.id}->${output.id}:0`,
        source: added.id,
        target: output.id,
        targetPort: 0,
      }),
    );

    // 文本被画布覆盖
    await waitFor(() => expect(result.current.text).toBe('$close'), { timeout: 2000 });

    const positions = result.current.state.nodes.map((node) => `${node.id}@${node.x},${node.y}`);
    // 文本触发的校验也会打 /ast，但**画布是最后改动方**，结论不许重铺画布：
    // 否则用户刚摆好的位置和选中的积木会在 400ms 后被无声清掉。
    await waitFor(() => expect(result.current.sync.expression).toBe('$close'), { timeout: 2000 });

    expect(result.current.compiled.expression).toBe('$close');
    expect(result.current.state.nodes.map((node) => `${node.id}@${node.x},${node.y}`)).toEqual(
      positions,
    );
    expect(result.current.state.selectedId).toBe(added.id);
  });

  it('只挪积木（表达式没变）：不重新请求 /ast', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);
    await load(result, DSL);
    postMock.mockClear();

    const output = result.current.state.nodes.find((node) => node.kind === 'output') as {
      id: string;
    };
    act(() => result.current.move(output.id, 999, 999));
    await flush();

    expect(postMock).not.toHaveBeenCalledWith('/factors/ast', expect.anything());
    expect(result.current.state.nodes.find((node) => node.id === output.id)).toMatchObject({
      x: 999,
      y: 999,
    });
  });

  it('归一：文本换成服务端规范写法；回退：丢弃文本回到画布结构', async () => {
    postMock.mockImplementation((url: string) => {
      if (url === '/factors/ast') return Promise.resolve({ expression: DSL, ast: AST, translated: true });
      return Promise.resolve({});
    });
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);
    await load(result, LEGACY);

    // 打开后文本已是归一写法
    expect(result.current.text).toBe(DSL);

    act(() => result.current.setText('Ts_Slope($close, 10) / $close'));
    // 必须等**结论归属**变成这段文本；只看 normalized 会立刻命中上一段的结论
    await waitFor(() => expect(result.current.sync.expression).toBe('Ts_Slope($close, 10) / $close'), {
      timeout: 2000,
    });

    act(() => result.current.formatText());
    expect(result.current.text).toBe(DSL);

    act(() => result.current.setText('坏掉的一行'));
    act(() => result.current.revertToCanvas());
    expect(result.current.text).toBe('(Ts_Slope($close,10) / $close)');
  });

  it('归一：结论不属于当前文本时不动文本（否则会抹掉刚敲的改动）', async () => {
    const { result } = renderHook(() => useFactorEditor());
    await ready(result);
    await load(result, DSL);

    // 敲一个新表达式，防抖还没触发：此时 normalized 仍是上一段文本的 DSL
    act(() => result.current.setText(`${DSL}+1`));
    expect(result.current.sync.normalized).toBe(DSL);

    act(() => result.current.formatText());
    expect(result.current.text).toBe(`${DSL}+1`);

    // 新结论回来后才允许归一
    await waitFor(() => expect(result.current.sync.expression).toBe(`${DSL}+1`), { timeout: 2000 });
    act(() => result.current.formatText());
    expect(result.current.text).toBe(DSL);
  });
});
