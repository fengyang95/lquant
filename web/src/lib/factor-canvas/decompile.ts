/**
 * 服务端 AST JSON → 画布节点/连线。
 *
 * 前端**不重写**词法/语法分析器（两份必然漂移），只做形状映射：
 *   field → 字段积木；number → 常数积木；call → 算子积木；
 *   binary → 中缀积木；unary → 一元取反积木。
 *
 * 一元负号必须是独立积木而非改写成 `0-x`：canonical 不折叠这种改写，
 * 改写会让「打开 → 保存」的往返不再稳定（P0 有专门的回归测试）。
 *
 * 有损映射（如窗口参数写成了表达式）一律记 warning，UI 据此禁止保存 ——
 * 宁可让用户看到"这个因子画布表达不了"，也不要静默换一套语义。
 */
import { findInfix, findOp, paramFallback } from './catalog';
import type { AstNode, CanvasEdge, CanvasNode, Catalog } from './types';

export type DecompileResult = {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  warnings: string[];
};

export function decompileAst(
  ast: AstNode,
  catalog: Catalog,
  outputLabel = '因子输出',
): DecompileResult {
  const nodes: CanvasNode[] = [];
  const edges: CanvasEdge[] = [];
  const warnings: string[] = [];
  let seq = 0;
  const nextId = (prefix: string) => `${prefix}-${seq++}`;

  const connect = (source: string, target: string, port: number) => {
    edges.push({ id: `e-${target}-${port}-${seq++}`, source, target, targetPort: port });
  };

  const build = (node: AstNode): string | null => {
    if (node.kind === 'field') {
      const id = nextId('field');
      nodes.push({ id, kind: 'field', field: node.name });
      return id;
    }
    if (node.kind === 'number') {
      const id = nextId('const');
      nodes.push({ id, kind: 'constant', value: node.value });
      return id;
    }
    if (node.kind === 'unary') {
      if (!findInfix(catalog, node.op, 1)) {
        warnings.push(`画布不支持的一元运算：${node.op}`);
        return null;
      }
      const id = nextId('unary');
      nodes.push({ id, kind: 'infix', op: node.op, arity: 1 });
      const arg = build(node.arg);
      if (arg) connect(arg, id, 0);
      return id;
    }
    if (node.kind === 'binary') {
      if (!findInfix(catalog, node.op, 2)) {
        warnings.push(`画布不支持的中缀运算：${node.op}`);
        return null;
      }
      const id = nextId('binary');
      nodes.push({ id, kind: 'infix', op: node.op, arity: 2 });
      const left = build(node.left);
      const right = build(node.right);
      if (left) connect(left, id, 0);
      if (right) connect(right, id, 1);
      return id;
    }

    const def = findOp(catalog, node.name);
    if (!def) {
      warnings.push(`未注册的算子：${node.name}`);
      return null;
    }
    if (def.series_arity < 0) {
      warnings.push(`${def.label}：服务端无法自省其签名，画布不支持该算子`);
      return null;
    }
    const seriesArgs = node.args.slice(0, def.series_arity);
    const scalarArgs = node.args.slice(def.series_arity);
    const params: Record<string, number> = {};
    def.params.forEach((param, index) => {
      const arg = scalarArgs[index];
      // 源表达式没写的参数**不要补出来**。补了会让 canonical_id 变
      // （Ts_Quantile($close,20) 与 Ts_Quantile($close,20,0.8) 语义相同、
      //  canonical 不同），打开再保存就成了另一个因子，去重层对不上。
      if (arg === undefined) return;
      if (arg.kind === 'number') {
        params[param.name] = arg.value;
        return;
      }
      // 参数写成了表达式（如 Ts_Mean($close, Ts_Max($close,5))）——
      // 画布的参数控件表达不了，只能按默认值近似，并明确告警。
      warnings.push(`${def.label}：参数「${param.name}」是表达式，画布只能按默认值近似`);
      params[param.name] = paramFallback(param);
    });
    if (node.args.length > def.series_arity + def.params.length) {
      warnings.push(`${def.label}：参数个数超出画布支持的范围`);
    }

    const id = nextId('op');
    nodes.push({ id, kind: 'op', op: def.name, params });
    seriesArgs.forEach((arg, port) => {
      const source = build(arg);
      if (source) connect(source, id, port);
    });
    if (seriesArgs.length < def.series_arity) {
      warnings.push(`${def.label}：缺少输入端口 ${seriesArgs.length + 1}/${def.series_arity}`);
    }
    return id;
  };

  const rootId = build(ast);
  const outputId = nextId('output');
  nodes.push({ id: outputId, kind: 'output', label: outputLabel });
  if (rootId) {
    connect(rootId, outputId, 0);
  } else {
    warnings.push('表达式无法映射到画布');
  }
  return { nodes, edges, warnings };
}
