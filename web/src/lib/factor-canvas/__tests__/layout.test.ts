/**
 * 自动布局回归：反解析出来的积木不能叠在一起。
 *
 * 这是「打开已有因子」与「表达式直编」可用性的前提 —— 少了布局，
 * 所有节点坐标都是 undefined，React Flow 会把它们全画在原点。
 */
import { describe, expect, it } from 'vitest';

import { decompileAst } from '../decompile';
import { LAYER_X_GAP, layoutNodes } from '../layout';
import type { AstNode, CanvasEdge, CanvasNode } from '../types';
import { FIXTURE_CATALOG } from './fixtures';

/** 与 useFactorEditor 的调用口径一致：先反解析，再布局 */
function layoutOf(ast: AstNode, previous: CanvasNode[] = []) {
  const { nodes, edges } = decompileAst(ast, FIXTURE_CATALOG);
  return { nodes: layoutNodes(nodes, edges, previous), edges };
}

const MEAN: AstNode = {
  kind: 'call',
  name: 'Ts_Mean',
  args: [{ kind: 'field', name: 'close' }, { kind: 'number', value: 5 }],
};

/** `Ts_Mean($close,5)/$close` */
const DIV: AstNode = {
  kind: 'binary',
  op: '/',
  left: MEAN,
  right: { kind: 'field', name: 'close' },
};

function overlaps(nodes: CanvasNode[]): string[] {
  const bad: string[] = [];
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      if (nodes[i].x === nodes[j].x && nodes[i].y === nodes[j].y) {
        bad.push(`${nodes[i].id} 与 ${nodes[j].id} 重叠于 (${nodes[i].x}, ${nodes[i].y})`);
      }
    }
  }
  return bad;
}

describe('layoutNodes', () => {
  it('每个节点都拿到坐标，且互不重叠', () => {
    const { nodes } = layoutOf(DIV);
    expect(nodes.length).toBeGreaterThan(3);
    for (const node of nodes) {
      expect(node.x).toBeTypeOf('number');
      expect(node.y).toBeTypeOf('number');
    }
    expect(overlaps(nodes)).toEqual([]);
  });

  it('输出积木在最右列，上游按层向左展开', () => {
    const { nodes, edges } = layoutOf(DIV);
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const output = nodes.find((n) => n.kind === 'output');
    expect(output).toBeDefined();
    const outputX = output?.x ?? 0;
    // 输出直连的那一级（除法）应当正好差一层
    const div = nodes.find((n) => n.kind === 'infix');
    expect(div).toBeDefined();
    expect(outputX - (div?.x ?? 0)).toBe(LAYER_X_GAP);
    // 除法的输入（Ts_Mean 与 $close）都比它更靠左
    for (const edge of edges) {
      if (edge.target === div?.id) {
        expect(byId.get(edge.source)?.x ?? 0).toBeLessThan(div?.x ?? 0);
      }
    }
  });

  it('同层节点纵向错开，不共用坐标', () => {
    const { nodes } = layoutOf(DIV);
    // 按列（x）分组：同一列里不允许有节点共用 y
    const columns = new Map<number, CanvasNode[]>();
    for (const node of nodes) {
      const column = columns.get(node.x ?? 0) ?? [];
      column.push(node);
      columns.set(node.x ?? 0, column);
    }
    // Ts_Mean 与右侧的 $close 都直接喂给除法，必然同列 —— 这一列得有多个节点
    const widest = Math.max(...[...columns.values()].map((column) => column.length));
    expect(widest).toBeGreaterThan(1);
    for (const column of columns.values()) {
      expect(new Set(column.map((n) => n.y)).size).toBe(column.length);
    }
  });

  it('签名相同的节点沿用旧坐标（改参数不整体跳位）', () => {
    const first = layoutOf(DIV);
    const moved = first.nodes.map((n) =>
      n.kind === 'op' ? { ...n, x: 1234, y: 567 } : n,
    );
    const second = layoutOf(DIV, moved);
    const op = second.nodes.find((n) => n.kind === 'op');
    expect(op?.x).toBe(1234);
    expect(op?.y).toBe(567);
  });

  it('签名不同不沿用旧坐标（避免张冠李戴）', () => {
    const first = layoutOf(DIV);
    // 把旧坐标挂在同一个 id 上，但积木种类不同 —— 不能复用
    const wrong: CanvasNode[] = first.nodes.map((n) =>
      n.id === 'op-2' ? { ...n, kind: 'constant', op: undefined, value: 9, x: 999, y: 999 } : n,
    );
    const second = layoutOf(DIV, wrong);
    const op = second.nodes.find((n) => n.id === 'op-2');
    expect(op?.x).not.toBe(999);
  });

  it('连不到输出的孤立积木也有坐标', () => {
    const { nodes, edges } = decompileAst(MEAN, FIXTURE_CATALOG);
    // 手工加一个悬空常数：不在任何连线上
    const orphan: CanvasNode = { id: 'orphan', kind: 'constant', value: 3 };
    const laid = layoutNodes([...nodes, orphan], edges as CanvasEdge[]);
    const found = laid.find((n) => n.id === 'orphan');
    expect(found?.x).toBeTypeOf('number');
    expect(found?.y).toBeTypeOf('number');
    expect(overlaps(laid)).toEqual([]);
  });

  it('空画布原样返回', () => {
    expect(layoutNodes([], [])).toEqual([]);
  });
});
