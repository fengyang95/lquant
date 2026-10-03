import { describe, expect, it } from 'vitest';

import {
  EMPTY_CATALOG,
  defaultParams,
  findField,
  findInfix,
  findOp,
  groupOps,
  inputArity,
  isSupported,
  nodeTitle,
  paramFallback,
  paramLabel,
} from '../catalog';
import { FIXTURE_CATALOG } from './fixtures';

describe('目录驱动（前端不持有算子定义）', () => {
  it('空目录产出零积木', () => {
    expect(groupOps(EMPTY_CATALOG)).toEqual([]);
    expect(findOp(EMPTY_CATALOG, 'Ts_Mean')).toBeUndefined();
    expect(findInfix(EMPTY_CATALOG, '+')).toBeUndefined();
    expect(findField(EMPTY_CATALOG, 'close')).toBeUndefined();
    expect(EMPTY_CATALOG.fields).toEqual([]);
  });

  it('按 category 分组，且只保留有算子的组', () => {
    const groups = groupOps(FIXTURE_CATALOG);
    expect(groups.map((g) => g.category)).toEqual(['TS', 'CS', 'EL']);
    expect(groups[0].items.map((o) => o.name)).toEqual(['Ts_Mean', 'Ts_Corr', 'Ts_Quantile']);
    // 空目录下不产生空组
    expect(groups.every((g) => g.items.length > 0)).toBe(true);
  });

  it('label 原样透传 —— 画布展示的语义就是引擎的语义', () => {
    expect(findOp(FIXTURE_CATALOG, 'Greater')?.label).toBe('逐元素取大');
    expect(findInfix(FIXTURE_CATALOG, '>', 2)?.label).toBe('大于（输出 0/1）');
  });

  it('一元与二元减号靠 arity 区分，不会互相串台', () => {
    expect(findInfix(FIXTURE_CATALOG, '-', 2)?.label).toBe('减法');
    expect(findInfix(FIXTURE_CATALOG, '-', 1)?.label).toBe('取相反数');
    expect(findInfix(FIXTURE_CATALOG, '-', 3)).toBeUndefined();
  });

  it('签名不可自省的算子被判定为不可用，且不进面板', () => {
    const opaque = findOp(FIXTURE_CATALOG, 'OpaqueOp');
    expect(opaque?.series_arity).toBe(-1);
    expect(isSupported(opaque)).toBe(false);
    expect(isSupported(findOp(FIXTURE_CATALOG, 'Ts_Mean'))).toBe(true);

    // 面板里不能出现它 —— 放进去只能生成坏表达式
    const listed = groupOps(FIXTURE_CATALOG).flatMap((g) => g.items.map((o) => o.name));
    expect(listed).not.toContain('OpaqueOp');

    // 元数按 0 报（不能是 -1，否则画布会画出负数的端口）
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'OpaqueOp' })).toBe(0);
  });

  it('inputArity 反映服务端元数', () => {
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'field' })).toBe(0);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'constant' })).toBe(0);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'output' })).toBe(1);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'Ts_Corr' })).toBe(2);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'If' })).toBe(3);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'infix', op: '/', arity: 2 })).toBe(2);
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'infix', op: '-', arity: 1 })).toBe(1);
    // 未注册算子 → 0，compile 会据此报"未注册"
    expect(inputArity(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'Nope' })).toBe(0);
  });

  it('参数默认值：窗口用画布缺省，其余用服务端默认', () => {
    const quantile = findOp(FIXTURE_CATALOG, 'Ts_Quantile');
    expect(quantile && defaultParams(quantile)).toEqual({ n: 5, q: 0.8 });
  });

  it('窗口缺省口径只有一处（节点工厂与编译器不许各给一个数）', () => {
    // 服务端如实报告 n 无默认值；画布统一按 DEFAULT_WINDOW 兜底
    expect(paramFallback({ name: 'n', type: 'window', required: true, default: null })).toBe(5);
    // 非窗口参数没有服务端默认时兜 1，而不是窗口缺省
    expect(paramFallback({ name: 'p', type: 'number', required: true, default: null })).toBe(1);
    // 服务端给了默认就照用
    expect(paramFallback({ name: 'q', type: 'number', required: false, default: 0.8 })).toBe(0.8);
  });

  it('参数标签区分窗口与普通数值', () => {
    expect(paramLabel({ name: 'n', type: 'window', required: true, default: null })).toBe('窗口长度');
    expect(paramLabel({ name: 'q', type: 'number', required: false, default: 0.8 })).toBe('数值');
  });

  it('nodeTitle 优先用服务端中文名', () => {
    expect(nodeTitle(FIXTURE_CATALOG, { id: 'a', kind: 'field', field: 'close' })).toBe('收盘价');
    expect(nodeTitle(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'Ts_Mean' })).toBe('时序均值');
    expect(nodeTitle(FIXTURE_CATALOG, { id: 'a', kind: 'constant', value: 3 })).toBe('3');
    expect(nodeTitle(FIXTURE_CATALOG, { id: 'a', kind: 'op', op: 'Nope' })).toBe('Nope');
  });
});
