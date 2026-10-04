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
