/**
 * 反解析出来的 DAG 自动布局。
 *
 * `decompileAst` 只产出拓扑（节点 + 连线），**不带坐标**。少了这一步，
 * 「打开已有因子」和「表达式直编」解析出来的积木会全部叠在原点 ——
 * 用户看到的是一摞卡片，画布等于不可用。
 *
 * 采用最简的分层布局：以因子输出为根，沿入边反向可达的节点按「到输出的
 * 最长路径」定层，输出在最右；同层按节点出现顺序纵向排开。纯函数、稳定
 * 输出，方便直接跑测试。
 */
import type { CanvasEdge, CanvasNode } from './types';

/** 相邻两层的横向间距（节点宽 190，留出连线空间） */
export const LAYER_X_GAP = 260;
/** 同层相邻节点的纵向间距（节点高约 60） */
export const LAYER_Y_GAP = 104;
export const ORIGIN_X = 60;
export const ORIGIN_Y = 60;

/**
 * 节点身份签名。
 *
 * 坐标复用只在「同一个积木」上生效 —— 反解析的节点 id 是按遍历顺序编号的
 * （field-0、op-1…），单看 id 会在结构变化后张冠李戴，把参数 A 的位置留给
 * 参数 B。带上种类与关键字段后，复用才是安全的。
 */
function signature(node: CanvasNode): string {
  return [
    node.kind,
    node.op ?? '',
    node.arity ?? '',
    node.field ?? '',
    node.value ?? '',
    node.label ?? '',
  ].join('|');
}

/**
 * 分层布局。`previous` 给定时，签名相同的节点沿用旧坐标 ——
 * 这样在表达式里改一个参数，画布上的积木不会整体跳位。
 */
export function layoutNodes(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  previous: CanvasNode[] = [],
): CanvasNode[] {
  if (nodes.length === 0) return nodes;

  const output = nodes.find((node) => node.kind === 'output');
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const outbound = new Map<string, string[]>();
  const inbound = new Map<string, string[]>();
  for (const edge of edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue;
    const outs = outbound.get(edge.source);
    if (outs) outs.push(edge.target);
    else outbound.set(edge.source, [edge.target]);
    const ins = inbound.get(edge.target);
    if (ins) ins.push(edge.source);
    else inbound.set(edge.target, [edge.source]);
  }

  // 1) 从输出沿入边反向可达的节点 —— 只有这些能定层
  const reachable = new Set<string>();
  if (output) {
    const stack = [output.id];
    while (stack.length > 0) {
      const id = stack.pop() as string;
      if (reachable.has(id)) continue;
      reachable.add(id);
      stack.push(...(inbound.get(id) ?? []));
    }
  }

  // 2) rank = 到输出的最长路径（输出为 0，越靠上游越大）
  const ranks = new Map<string, number>();
  const visiting = new Set<string>();
  const rankOf = (id: string): number => {
    const cached = ranks.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0; // 环兜底：连接层已挡，这里只防布局死循环
    visiting.add(id);
    let best = 0;
    for (const target of outbound.get(id) ?? []) {
      best = Math.max(best, rankOf(target) + 1);
    }
    visiting.delete(id);
    ranks.set(id, best);
    return best;
  };
  // 每个可达节点都要算 —— 只从输出算起的话，输出没有出边，递归不下去，
  // 上游节点会全部停在 0 层，布局退化成"输出与所有积木同列"。
  let maxRank = 0;
  for (const id of reachable) maxRank = Math.max(maxRank, rankOf(id));

  // 3) 落坐标。孤立的积木（连不到输出）排在最左列的下方，不跟主链挤在一起
  const prevByKey = new Map(previous.map((node) => [node.id, node]));
  const usedY = new Map<number, number>();
  return nodes.map((node) => {
    const prev = prevByKey.get(node.id);
    if (prev && signature(prev) === signature(node) && prev.x != null && prev.y != null) {
      return { ...node, x: prev.x, y: prev.y };
    }
    const rank = reachable.has(node.id) ? ranks.get(node.id) ?? 0 : maxRank;
    const index = usedY.get(rank) ?? 0;
    usedY.set(rank, index + 1);
    return {
      ...node,
      x: ORIGIN_X + (maxRank - rank) * LAYER_X_GAP,
      y: ORIGIN_Y + index * LAYER_Y_GAP,
    };
  });
}
