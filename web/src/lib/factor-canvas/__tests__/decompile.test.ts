import { describe, expect, it } from 'vitest';

import { compileCanvas } from '../compile';
import { decompileAst } from '../decompile';
import type { AstNode } from '../types';
import { FIXTURE_CATALOG as CATALOG } from './fixtures';

const AST_TS_MEAN: AstNode = {
  kind: 'call',
  name: 'Ts_Mean',
  args: [
    { kind: 'field', name: 'close' },
    { kind: 'number', value: 5 },
  ],
};

const AST_RANK_PIPELINE: AstNode = {
  kind: 'call',
  name: 'Rank',
  args: [
    {
      kind: 'binary',
      op: '-',
      left: {
        kind: 'binary',
        op: '/',
        left: AST_TS_MEAN,
        right: { kind: 'field', name: 'close' },
      },
      right: { kind: 'number', value: 1 },
    },
  ],
};

describe('AST → 画布', () => {
  it('字段与常数落成对应积木', () => {
    const r = decompileAst({ kind: 'field', name: 'close' }, CATALOG);
    expect(r.warnings).toEqual([]);
    expect(r.nodes.map((n) => n.kind)).toEqual(['field', 'output']);
    expect(r.nodes[0].field).toBe('close');
    expect(r.edges).toHaveLength(1);
    expect(r.edges[0].targetPort).toBe(0);
  });

  it('算子调用按元数拆分序列输入与标量参数', () => {
    const r = decompileAst(AST_TS_MEAN, CATALOG);
    const op = r.nodes.find((n) => n.kind === 'op');
    expect(op?.op).toBe('Ts_Mean');
    expect(op?.params).toEqual({ n: 5 });
    // 序列输入接成一条边，标量参数不产生边
    expect(r.edges.filter((e) => e.target === op?.id)).toHaveLength(1);
  });

  it('二元运算落成 arity=2 的中缀积木', () => {
    const r = decompileAst(
      { kind: 'binary', op: '/', left: { kind: 'field', name: 'close' }, right: { kind: 'field', name: 'open' } },
      CATALOG,
    );
    const node = r.nodes.find((n) => n.kind === 'infix');
    expect(node?.op).toBe('/');
    expect(node?.arity).toBe(2);
    expect(r.edges.filter((e) => e.target === node?.id).map((e) => e.targetPort)).toEqual([0, 1]);
  });

  it('一元负号落成 arity=1 的中缀积木（不改写成 0-x）', () => {
    const r = decompileAst(
      { kind: 'unary', op: '-', arg: { kind: 'field', name: 'close' } },
      CATALOG,
    );
    const node = r.nodes.find((n) => n.kind === 'infix');
    expect(node?.arity).toBe(1);
    expect(r.edges.filter((e) => e.target === node?.id)).toHaveLength(1);
    // 关键：往返后仍是一元负号，而不是 (0-$close)
    expect(compileCanvas(r.nodes, r.edges, CATALOG).expression).toBe('-($close)');
  });

  it('嵌套表达式往返后语义一致', () => {
    const r = decompileAst(AST_RANK_PIPELINE, CATALOG);
    expect(r.warnings).toEqual([]);
    expect(compileCanvas(r.nodes, r.edges, CATALOG).expression)
      .toBe('Rank(((Ts_Mean($close,5) / $close) - 1))');
  });

  it('未注册算子不静默：出 warning 且不接出根节点', () => {
    const r = decompileAst({ kind: 'call', name: 'Nope', args: [] }, CATALOG);
    expect(r.warnings.join()).toContain('未注册的算子：Nope');
    expect(r.warnings.join()).toContain('表达式无法映射到画布');
    // 仍然给出输出积木，便于用户看到"这里断了"
    expect(r.nodes.some((n) => n.kind === 'output')).toBe(true);
  });

  it('参数写成表达式时明确告警而不是静默换值', () => {
    const ast: AstNode = {
      kind: 'call',
      name: 'Ts_Mean',
      args: [
        { kind: 'field', name: 'close' },
        { kind: 'call', name: 'Rank', args: [{ kind: 'field', name: 'close' }] },
      ],
    };
    const r = decompileAst(ast, CATALOG);
    expect(r.warnings.join()).toContain('参数「n」是表达式');
    const op = r.nodes.find((n) => n.kind === 'op');
    expect(op?.params).toEqual({ n: 5 }); // 回落到画布默认窗口
  });

  it('源表达式省略的可选参数不被补出来，往返 canonical 稳定', () => {
    const ast: AstNode = {
      kind: 'call',
      name: 'Ts_Quantile',
      args: [
        { kind: 'field', name: 'close' },
        { kind: 'number', value: 20 },
      ],
    };
    const r = decompileAst(ast, CATALOG);
    expect(r.warnings).toEqual([]);
    const op = r.nodes.find((n) => n.kind === 'op');
    // 只有源里写了的 n，没有把 q 的默认值补出来
    expect(op?.params).toEqual({ n: 20 });
    expect(compileCanvas(r.nodes, r.edges, CATALOG).expression).toBe('Ts_Quantile($close,20)');
  });

  it('序列输入不足时告警', () => {
    const r = decompileAst({ kind: 'call', name: 'Ts_Corr', args: [{ kind: 'field', name: 'close' }] }, CATALOG);
    expect(r.warnings.join()).toContain('缺少输入端口 2/2');
  });
});
