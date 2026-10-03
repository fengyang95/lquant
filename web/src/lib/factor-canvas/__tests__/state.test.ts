import { describe, expect, it } from 'vitest';

import {
  addNode,
  connect,
  createNode,
  disconnect,
  isDirty,
  loadState,
  missingPorts,
  moveNode,
  newCanvas,
  nextNodeId,
  removeNode,
  selectNode,
  setParam,
  updateNode,
} from '../state';
import type { CanvasEdge, CanvasNode } from '../types';
import { FIXTURE_CATALOG as CATALOG } from './fixtures';

const edge = (source: string, target: string, targetPort: number): CanvasEdge => ({
  id: `${source}->${target}:${targetPort}`,
  source,
  target,
  targetPort,
});

describe('画布状态（不可变更新）', () => {
  it('新建画布只有因子输出积木', () => {
    const state = newCanvas();
    expect(state.nodes).toHaveLength(1);
    expect(state.nodes[0].kind).toBe('output');
    expect(state.edges).toEqual([]);
    expect(isDirty(state, '')).toBe(false);
  });

  it('nextNodeId 不与既有 id 冲突', () => {
    const nodes: CanvasNode[] = [{ id: 'field-0', kind: 'field' }];
    expect(nextNodeId(nodes, 'field')).toBe('field-1');
    expect(nextNodeId([...nodes, { id: 'field-1', kind: 'field' }], 'field')).toBe('field-2');
  });

  it('createNode 按目录给默认值', () => {
    expect(createNode(CATALOG, 'field', 0, 0).field).toBe('open');
    expect(createNode(CATALOG, 'constant', 0, 0).value).toBe(1);
    expect(createNode(CATALOG, 'op', 0, 0, 'Ts_Quantile').params).toEqual({ n: 5, q: 0.8 });
    expect(createNode(CATALOG, 'infix', 0, 0, '+')).toMatchObject({ op: '+', arity: 2 });
    // 未知算子不抛错，留给 compile 报"未注册"
    expect(createNode(CATALOG, 'op', 0, 0, 'Nope').op).toBeUndefined();
  });

  it('增删节点不修改原状态，且删节点连带删连线', () => {
    const base = loadState([{ id: 'a', kind: 'field' }, { id: 'out', kind: 'output' }],
      [edge('a', 'out', 0)], '');
    const added = addNode(base, { id: 'b', kind: 'constant', value: 2 });
    expect(base.nodes).toHaveLength(2);            // 原状态未变
    expect(added.nodes).toHaveLength(3);
    expect(added.selectedId).toBe('b');

    const removed = removeNode(added, 'a');
    expect(removed.nodes.map((n) => n.id)).toEqual(['out', 'b']);
    expect(removed.edges).toEqual([]);              // 悬空连线一并清掉
  });

  it('移动节点只改坐标', () => {
    const base = loadState([{ id: 'a', kind: 'field', x: 0, y: 0 }], [], '');
    const moved = moveNode(base, 'a', 12, 34);
    expect(moved.nodes[0]).toMatchObject({ x: 12, y: 34, kind: 'field' });
    expect(base.nodes[0]).toMatchObject({ x: 0, y: 0 });
  });

  it('同一目标端口只保留一条入边', () => {
    const base = loadState(
      [{ id: 'a', kind: 'field' }, { id: 'b', kind: 'field' }, { id: 'm', kind: 'op', op: 'Ts_Mean' }],
      [edge('a', 'm', 0)],
      '',
    );
    const next = connect(base, edge('b', 'm', 0));
    expect(next.edges).toHaveLength(1);
    expect(next.edges[0].source).toBe('b');
  });

  it('拒绝自连与成环', () => {
    const base = loadState(
      [{ id: 'a', kind: 'op', op: 'Ts_Mean' }, { id: 'b', kind: 'op', op: 'Ts_Mean' }],
      [edge('a', 'b', 0)],
      '',
    );
    expect(connect(base, edge('a', 'a', 0)).edges).toHaveLength(1);   // 自连被拒
    expect(connect(base, edge('b', 'a', 0)).edges).toHaveLength(1);   // 成环被拒
    expect(connect(base, edge('b', 'a', 0))).toBe(base);              // 原样返回
  });

  it('断开连线只删那一条', () => {
    const base = loadState([], [edge('a', 'm', 0), edge('b', 'm', 1)], '');
    const next = disconnect(base, 'a->m:0');
    expect(next.edges.map((e) => e.id)).toEqual(['b->m:1']);
  });

  it('参数与选中状态更新保持不可变', () => {
    const base = loadState([{ id: 'm', kind: 'op', op: 'Ts_Mean', params: { n: 5 } }], [], '');
    const patched = setParam(base, 'm', 'n', 20);
    expect(patched.nodes[0].params).toEqual({ n: 20 });
    expect(base.nodes[0].params).toEqual({ n: 5 });  // 原状态未变

    const renamed = updateNode(base, 'm', { op: 'Ts_Sum' });
    expect(renamed.nodes[0].op).toBe('Ts_Sum');
    expect(selectNode(base, 'm').selectedId).toBe('m');
    expect(selectNode(base, null).selectedId).toBeNull();
  });

  it('missingPorts 报出还差哪些输入口', () => {
    const corr: CanvasNode = { id: 'c', kind: 'op', op: 'Ts_Corr' };
    expect(missingPorts(CATALOG, corr, [])).toEqual([0, 1]);
    expect(missingPorts(CATALOG, corr, [edge('a', 'c', 0)])).toEqual([1]);
    expect(missingPorts(CATALOG, corr, [edge('a', 'c', 0), edge('b', 'c', 1)])).toEqual([]);
    // 字段积木没有输入口
    expect(missingPorts(CATALOG, { id: 'f', kind: 'field', field: 'close' }, [])).toEqual([]);
  });

  it('脏标记以保存时的表达式为基线', () => {
    const state = loadState([], [], 'Rank($close)');
    expect(isDirty(state, 'Rank($close)')).toBe(false);
    expect(isDirty(state, 'Rank($open)')).toBe(true);
  });
});
