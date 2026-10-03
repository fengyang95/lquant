/**
 * 画布 DAG → lquant DSL 表达式。
 *
 * 与 yinzi 原版最大的不同：**不需要 60 分支的 switch**。lquant 的 DSL 语法
 * 极简（`dsl/parser.py`）：中缀 `+ - * / < >` 加统一的 `Name(arg, ...)` 调用。
 * 于是规则只有三条：
 *   1. 字段 → `$name`
 *   2. 常数 → 字面量
 *   3. 四则/比较 → 中缀；其余算子 → `Name(序列输入..., 标量参数...)`
 *
 * 算子元数与参数顺序全部来自服务端目录，新增算子不用改这里。
 *
 * 出错路径统一返回 `null` 这个**非法**字面量：错误必然伴随 warning，
 * UI 据此禁止保存 —— 宁可显式坏掉，也不要静默生成语义不同的表达式。
 */
import { findInfix, findOp, nodeTitle, paramFallback } from './catalog';
import type { CanvasEdge, CanvasNode, Catalog, CompileResult } from './types';

/** 兜底字面量：非法 DSL，只在伴随 warning 的错误路径出现 */
const BROKEN = 'null';

export function formatNumber(value: number): string {
  // 非有限值在 UI 层不可达（数字输入框清空得到 0），但真到了这里宁可给
  // 一个显眼的非法值，也不要静默变成 0 —— 那是无声的语义篡改。
  if (!Number.isFinite(value)) return BROKEN;
  if (Number.isInteger(value)) return String(value);
  const rounded = Number(value.toFixed(8));
  // 1e-9 这类极小值不能塌成 0；DSL 词法接受科学计数法（lexer.py 的 number 分支）
  if (rounded === 0) return value.toExponential();
  return String(rounded);
}

export function compileCanvas(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  catalog: Catalog,
): CompileResult {
  const warnings = new Set<string>();
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const output = nodes.find((n) => n.kind === 'output');
  if (!output) return { expression: '', warnings: ['未找到因子输出积木'] };

  // 入边按 target 索引，端口用 targetPort 定位
  const inbound = new Map<string, CanvasEdge[]>();
  for (const edge of edges) {
    const list = inbound.get(edge.target);
    if (list) list.push(edge);
    else inbound.set(edge.target, [edge]);
  }

  const memo = new Map<string, string>();
  const visiting = new Set<string>();

  const inputAt = (node: CanvasNode, port: number): string | null => {
    const edge = (inbound.get(node.id) ?? []).find((e) => e.targetPort === port);
    if (!edge) return null;
    const source = byId.get(edge.source);
    return source ? compile(source) : null;
  };

  const render = (node: CanvasNode): string => {
    if (node.kind === 'field') {
      if (!node.field) {
        warnings.add('字段积木未选择字段');
        return BROKEN;
      }
      return `$${node.field}`;
    }
    if (node.kind === 'constant') {
      const value = node.value ?? 0;
      if (!Number.isFinite(value)) {
        warnings.add('数字常数不是有效数值');
        return BROKEN;
      }
      return formatNumber(value);
    }
    if (node.kind === 'output') {
      const value = inputAt(node, 0);
      if (value === null) {
        warnings.add('因子输出：缺少输入');
        return '';
      }
      return value;
    }
    if (node.kind === 'infix') {
      const arity = node.arity ?? 2;
      const def = findInfix(catalog, node.op, arity);
      if (!def) {
        warnings.add(`未注册的运算：${node.op ?? '(空)'}（${arity} 元）`);
        return BROKEN;
      }
      if (def.arity === 1) {
        const only = inputAt(node, 0);
        if (only === null) {
          warnings.add(`${def.label}：缺少输入端口`);
          return BROKEN;
        }
        // 括号必需：`-a+b` 会被解析成 `(-a)+b`。`-(x)` 与原式 canonical 同口径。
        return `-(${only})`;
      }
      const left = inputAt(node, 0);
      const right = inputAt(node, 1);
      if (left === null || right === null) {
        warnings.add(`${def.label}：缺少输入端口`);
        return BROKEN;
      }
      return `(${left} ${def.token} ${right})`;
    }

    const def = findOp(catalog, node.op);
    if (!def) {
      warnings.add(`未注册的算子：${node.op ?? '(空)'}`);
      return BROKEN;
    }
    if (def.series_arity < 0) {
      warnings.add(`${def.label}：服务端无法自省其签名，画布不支持该算子`);
      return BROKEN;
    }
    const args: string[] = [];
    for (let port = 0; port < def.series_arity; port += 1) {
      const value = inputAt(node, port);
      if (value === null) {
        warnings.add(`${def.label}：缺少输入端口 ${port + 1}/${def.series_arity}`);
        return BROKEN;
      }
      args.push(value);
    }
    // 标量参数按服务端声明的顺序追加，但**只写到最后一个被显式赋值的参数为止**：
    // 源表达式省略掉的尾部可选参数不再补出来，否则 canonical_id 会变，
    // 打开再保存就成了另一个因子（去重层会对不上）。
    const values = def.params.map((param) => node.params?.[param.name]);
    let lastPresent = -1;
    values.forEach((value, index) => {
      if (value !== undefined) lastPresent = index;
    });
    for (let index = 0; index <= lastPresent; index += 1) {
      args.push(formatNumber(values[index] ?? paramFallback(def.params[index])));
    }
    return `${def.name}(${args.join(',')})`;
  };

  const compile = (node: CanvasNode): string => {
    const cached = memo.get(node.id);
    if (cached !== undefined) return cached;
    if (visiting.has(node.id)) {
      warnings.add(`${nodeTitle(catalog, node)}：检测到循环连线`);
      return BROKEN;
    }
    visiting.add(node.id);
    const expression = render(node);
    visiting.delete(node.id);
    memo.set(node.id, expression);
    return expression;
  };

  const expression = compile(output);
  return { expression, warnings: [...warnings] };
}
