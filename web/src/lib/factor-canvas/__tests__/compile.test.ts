import { describe, expect, it } from 'vitest';

import { defaultParams, findOp } from '../catalog';
import { compileCanvas, formatNumber } from '../compile';
import type { CanvasEdge, CanvasNode } from '../types';
import { FIXTURE_CATALOG as CATALOG } from './fixtures';

const edge = (source: string, target: string, targetPort: number): CanvasEdge => ({
  id: `${source}->${target}:${targetPort}`,
  source,
  target,
  targetPort,
});

const field = (id: string, name: string): CanvasNode => ({ id, kind: 'field', field: name });
const constant = (id: string, value: number): CanvasNode => ({ id, kind: 'constant', value });
/** 与真实流程一致：面板/反解析造出来的算子节点都带参数（createNode 会填默认值） */
const op = (id: string, name: string, params?: Record<string, number>): CanvasNode => {
  const def = findOp(CATALOG, name);
  return { id, kind: 'op', op: name, params: params ?? (def ? defaultParams(def) : {}) };
};
const infix = (id: string, token: string, arity = 2): CanvasNode => ({
  id,
  kind: 'infix',
  op: token,
  arity,
});
const OUTPUT: CanvasNode = { id: 'out', kind: 'output', label: '因子输出' };

const run = (nodes: CanvasNode[], edges: CanvasEdge[]) =>
  compileCanvas(nodes, edges, CATALOG);

describe('DAG → DSL 编译', () => {
  it('字段与常数直接落成字面量', () => {
    expect(run([field('f', 'close'), OUTPUT], [edge('f', 'out', 0)]).expression).toBe('$close');
    expect(run([constant('c', 5), OUTPUT], [edge('c', 'out', 0)]).expression).toBe('5');
  });

  it('单序列算子按服务端元数补窗口参数', () => {
    const r = run(
      [field('f', 'close'), op('m', 'Ts_Mean'), OUTPUT],
      [edge('f', 'm', 0), edge('m', 'out', 0)],
    );
    expect(r.expression).toBe('Ts_Mean($close,5)');
    expect(r.warnings).toEqual([]);
  });

  it('双序列算子按端口顺序出参', () => {
    const r = run(
      [field('a', 'close'), field('b', 'volume'), op('c', 'Ts_Corr', { n: 20 }), OUTPUT],
      [edge('a', 'c', 0), edge('b', 'c', 1), edge('c', 'out', 0)],
    );
    expect(r.expression).toBe('Ts_Corr($close,$volume,20)');
  });

  it('面板新建的算子带全参数（createNode 用服务端默认值填好）', () => {
    const r = run(
      [field('f', 'close'), op('q', 'Ts_Quantile'), OUTPUT],
      [edge('f', 'q', 0), edge('q', 'out', 0)],
    );
    expect(r.expression).toBe('Ts_Quantile($close,5,0.8)');
  });

  it('三序列算子（If）不吃窗口参数', () => {
    const r = run(
      [
        field('a', 'close'),
        field('b', 'open'),
        field('c', 'volume'),
        op('i', 'If'),
        OUTPUT,
      ],
      [edge('a', 'i', 0), edge('b', 'i', 1), edge('c', 'i', 2), edge('i', 'out', 0)],
    );
    expect(r.expression).toBe('If($close,$open,$volume)');
  });

  it('四则走中缀而不是函数调用', () => {
    const r = run(
      [field('a', 'close'), field('b', 'open'), infix('d', '/'), OUTPUT],
      [edge('a', 'd', 0), edge('b', 'd', 1), edge('d', 'out', 0)],
    );
    expect(r.expression).toBe('($close / $open)');
  });

  it('一元取反输出带括号的负号，保证优先级正确', () => {
    const r = run(
      [field('a', 'close'), infix('neg', '-', 1), OUTPUT],
      [edge('a', 'neg', 0), edge('neg', 'out', 0)],
    );
    expect(r.expression).toBe('-($close)');
  });

  it('嵌套 DAG 逐层展开', () => {
    const nodes = [
      field('c1', 'close'),
      op('m', 'Ts_Mean'),
      field('c2', 'close'),
      infix('div', '/'),
      constant('one', 1),
      infix('sub', '-'),
      op('rank', 'Rank'),
      OUTPUT,
    ];
    const edges = [
      edge('c1', 'm', 0),
      edge('m', 'div', 0),
      edge('c2', 'div', 1),
      edge('div', 'sub', 0),
      edge('one', 'sub', 1),
      edge('sub', 'rank', 0),
      edge('rank', 'out', 0),
    ];
    expect(run(nodes, edges).expression).toBe('Rank(((Ts_Mean($close,5) / $close) - 1))');
  });

  it('没有输出积木时明确报错', () => {
    const r = run([field('f', 'close')], []);
    expect(r.expression).toBe('');
    expect(r.warnings).toContain('未找到因子输出积木');
  });

  it('未注册算子不静默：出 warning 且表达式为非法字面量', () => {
    const r = run(
      [field('f', 'close'), op('x', 'Nope'), OUTPUT],
      [edge('f', 'x', 0), edge('x', 'out', 0)],
    );
    expect(r.warnings.join()).toContain('未注册的算子');
    expect(r.expression).toBe('null');
  });

  it('缺输入端口时报出第几个口', () => {
    const r = run(
      [field('a', 'close'), op('c', 'Ts_Corr'), OUTPUT],
      [edge('a', 'c', 0), edge('c', 'out', 0)],
    );
    expect(r.warnings.join()).toContain('缺少输入端口 2/2');
    expect(r.expression).toBe('null');
  });

  it('检测到环连线时不递归到爆栈', () => {
    const r = run(
      [op('a', 'Ts_Mean'), op('b', 'Ts_Mean'), OUTPUT],
      [edge('b', 'a', 0), edge('a', 'b', 0), edge('a', 'out', 0)],
    );
    expect(r.warnings.join()).toContain('循环连线');
    // 环处落成非法字面量，整体表达式不可用（UI 见 warning 即禁止保存）
    expect(r.expression).toContain('null');
  });

  it('输出积木缺输入时不产出表达式', () => {
    const r = run([OUTPUT], []);
    expect(r.expression).toBe('');
    expect(r.warnings.join()).toContain('因子输出：缺少输入');
  });

  it('源表达式省略的尾部可选参数不再补出来（canonical 往返稳定）', () => {
    // Ts_Quantile($close,20) 打开后原样回写，不该变成 Ts_Quantile($close,20,0.8)
    const r = run(
      [field('f', 'close'), op('q', 'Ts_Quantile', { n: 20 }), OUTPUT],
      [edge('f', 'q', 0), edge('q', 'out', 0)],
    );
    expect(r.expression).toBe('Ts_Quantile($close,20)');
    expect(r.warnings).toEqual([]);
  });

  it('签名不可自省的算子被拒用，而不是当成零参调用发出去', () => {
    const r = run([op('x', 'OpaqueOp'), OUTPUT], [edge('x', 'out', 0)]);
    expect(r.warnings.join()).toContain('无法自省其签名');
    expect(r.expression).toBe('null');
  });

  it('非有限常数报错而不是静默变成 0', () => {
    const r = run([constant('c', Number.NaN), OUTPUT], [edge('c', 'out', 0)]);
    expect(r.warnings.join()).toContain('数字常数不是有效数值');
    expect(r.expression).toBe('null');
  });
});

describe('formatNumber', () => {
  it('整数不带小数点，小数去掉浮点尾巴', () => {
    expect(formatNumber(5)).toBe('5');
    expect(formatNumber(0.8)).toBe('0.8');
    expect(formatNumber(1 / 3)).toBe('0.33333333');
  });

  it('极小值不塌成 0，非有限值不伪装成合法的 0', () => {
    // 1e-9 若被 toFixed(8) 抹成 0，就是无声的语义篡改
    expect(formatNumber(1e-9)).toBe('1e-9');
    expect(formatNumber(Number.NaN)).toBe('null');
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe('null');
  });
});
