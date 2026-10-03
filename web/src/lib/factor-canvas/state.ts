/**
 * 因子编辑器的画布状态：纯函数 + 不可变更新。
 *
 * 拆出来是为了可测 —— 参照 `app/backtests/workspace/state.ts` 的做法，
 * 把编辑语义从 React 组件里剥离，vitest 直接跑。
 */
import { defaultParams, findInfix, findOp, inputArity } from './catalog';
import type { CanvasEdge, CanvasNode, Catalog, NodeKind } from './types';

export type EditorState = {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  selectedId: string | null;
  /** 最近一次保存/打开时的表达式基线，用于脏标记 */
  baselineExpression: string;
};

export const EMPTY_STATE: EditorState = {
  nodes: [],
  edges: [],
  selectedId: null,
  baselineExpression: '',
};

export function nextNodeId(nodes: CanvasNode[], prefix: string): string {
  let n = nodes.length;
  let id = `${prefix}-${n}`;
  while (nodes.some((node) => node.id === id)) {
    n += 1;
    id = `${prefix}-${n}`;
  }
  return id;
}

/**
 * 按积木种类造节点（位置由调用方给）。
 *
 * `arity` 只对中缀积木有意义：`-` 同时是一元取反与二元减法，
 * 必须由面板显式告知要放哪一种。
 */
export function createNode(
  catalog: Catalog,
  kind: NodeKind,
  x: number,
  y: number,
  opName?: string,
  arity = 2,
): CanvasNode {
  const base = { id: '', kind, x, y } as CanvasNode;
  if (kind === 'field') return { ...base, field: catalog.fields[0]?.name };
  if (kind === 'constant') return { ...base, value: 1 };
  if (kind === 'output') return { ...base, label: '因子输出' };
  if (kind === 'infix') {
    const def = findInfix(catalog, opName, arity);
    return { ...base, op: def?.token ?? opName ?? '+', arity: def?.arity ?? arity };
  }
  const def = findOp(catalog, opName);
  return { ...base, op: def?.name, params: def ? defaultParams(def) : {} };
}

/** 未连接输入端口（用于画布提示"还差几个口"） */
export function missingPorts(catalog: Catalog, node: CanvasNode, edges: CanvasEdge[]): number[] {
  const arity = inputArity(catalog, node);
  const filled = new Set(
    edges.filter((e) => e.target === node.id).map((e) => e.targetPort),
  );
  return Array.from({ length: arity }, (_, i) => i).filter((i) => !filled.has(i));
}

export function addNode(state: EditorState, node: CanvasNode): EditorState {
  return { ...state, nodes: [...state.nodes, node], selectedId: node.id };
}

/** 删节点时连带删掉它的所有入边与出边，不留悬空连线 */
export function removeNode(state: EditorState, id: string): EditorState {
  return {
    ...state,
    nodes: state.nodes.filter((node) => node.id !== id),
    edges: state.edges.filter((edge) => edge.source !== id && edge.target !== id),
    selectedId: state.selectedId === id ? null : state.selectedId,
  };
}

export function moveNode(state: EditorState, id: string, x: number, y: number): EditorState {
  return {
    ...state,
    nodes: state.nodes.map((node) => (node.id === id ? { ...node, x, y } : node)),
  };
}

export function updateNode(
  state: EditorState,
  id: string,
  patch: Partial<CanvasNode>,
): EditorState {
  return {
    ...state,
    nodes: state.nodes.map((node) => (node.id === id ? { ...node, ...patch } : node)),
  };
}

export function setParam(state: EditorState, id: string, name: string, value: number): EditorState {
  return {
    ...state,
    nodes: state.nodes.map((node) =>
      node.id === id ? { ...node, params: { ...node.params, [name]: value } } : node,
    ),
  };
}

export function selectNode(state: EditorState, id: string | null): EditorState {
  return { ...state, selectedId: id };
}

/**
 * 连线：同一目标端口只保留一条（后者覆盖前者），
 * 且拒绝自连与成环 —— 环由 compile 兜底，但这里先挡掉更直观。
 */
export function connect(state: EditorState, edge: CanvasEdge): EditorState {
  if (edge.source === edge.target) return state;
  if (createsCycle(state.edges, edge)) return state;
  const kept = state.edges.filter(
    (e) => !(e.target === edge.target && e.targetPort === edge.targetPort),
  );
  return { ...state, edges: [...kept, edge] };
}

export function disconnect(state: EditorState, edgeId: string): EditorState {
  return { ...state, edges: state.edges.filter((edge) => edge.id !== edgeId) };
}

export function createsCycle(edges: CanvasEdge[], candidate: CanvasEdge): boolean {
  // 从 candidate.source 沿入边反向回溯，若能回到 target 则成环
  const byTarget = new Map<string, string[]>();
  for (const edge of [...edges, candidate]) {
    const list = byTarget.get(edge.target);
    if (list) list.push(edge.source);
    else byTarget.set(edge.target, [edge.source]);
  }
  const stack = [candidate.source];
  const seen = new Set<string>();
  while (stack.length > 0) {
    const current = stack.pop() as string;
    if (current === candidate.target) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(byTarget.get(current) ?? []));
  }
  return false;
}

export function loadState(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  baselineExpression: string,
): EditorState {
  return { nodes, edges, selectedId: null, baselineExpression };
}

export function isDirty(state: EditorState, expression: string): boolean {
  return expression !== state.baselineExpression;
}

/** 新建空画布：只放一个因子输出积木，其余靠拖积木补齐 */
export function newCanvas(): EditorState {
  return loadState(
    [{ id: 'output-0', kind: 'output', label: '因子输出', x: 640, y: 200 }],
    [],
    '',
  );
}
