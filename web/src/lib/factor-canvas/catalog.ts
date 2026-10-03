/**
 * 算子目录：从服务端拉取，画布据此生成积木。
 *
 * 前端**不硬编码任何算子名**。目录为空时画布就该是空的 —— 这条由
 * `__tests__/catalog.test.ts` 的「空目录产出零积木」断言钉死。
 * 语义分歧（Greater 取大 vs 比较、Ts_ArgMax 的 0/1 基准、Log 的 log1p
 * 口径）因此无从漂移：画布展示的 label 就是引擎的 label。
 */
import { get } from '@/lib/api';
import type { CanvasNode, Catalog, FieldDef, InfixDef, OpDef, OpParam } from './types';
import { DEFAULT_WINDOW } from './types';

type OpsResponse = { ops?: OpDef[]; infix?: InfixDef[] };

export const EMPTY_CATALOG: Catalog = { ops: [], infix: [], fields: [] };

/** 拉取完整目录：算子 + 中缀 + 字段（两个接口并行） */
export async function fetchCatalog(): Promise<Catalog> {
  const [opsRes, fields] = await Promise.all([
    get<OpsResponse>('/factors/ops'),
    get<FieldDef[]>('/factors/fields'),
  ]);
  return {
    ops: opsRes?.ops ?? [],
    infix: opsRes?.infix ?? [],
    fields: fields ?? [],
  };
}

export function findOp(catalog: Catalog, name: string | undefined): OpDef | undefined {
  if (!name) return undefined;
  return catalog.ops.find((o) => o.name === name);
}

/**
 * 算子是否可被画布安全使用。
 *
 * 服务端拿不到签名时返回 `series_arity = -1`（哨兵），此时无法确定元数，
 * 画布必须拒绝它 —— 当成零参算子发出去会生成坏表达式，而服务端静态检查
 * 不校验元数，要等到求值才炸。
 */
export function isSupported(op: OpDef | undefined): op is OpDef {
  return op !== undefined && op.series_arity >= 0;
}

/**
 * 中缀算子查找。`-` 同时是一元取反（arity 1）与二元减法（arity 2），
 * 必须带上 arity 才能唯一确定 —— 服务端也是这么声明的。
 */
export function findInfix(
  catalog: Catalog,
  token: string | undefined,
  arity = 2,
): InfixDef | undefined {
  if (!token) return undefined;
  return catalog.infix.find((i) => i.token === token && i.arity === arity);
}

export function findField(catalog: Catalog, name: string | undefined): FieldDef | undefined {
  if (!name) return undefined;
  return catalog.fields.find((f) => f.name === name);
}

/** 节点吃几个序列输入；未知 / 不可用节点返回 0（compile 会据此报警） */
export function inputArity(catalog: Catalog, node: CanvasNode): number {
  if (node.kind === 'output') return 1;
  if (node.kind === 'op') {
    const op = findOp(catalog, node.op);
    return isSupported(op) ? op.series_arity : 0;
  }
  if (node.kind === 'infix') return findInfix(catalog, node.op, node.arity ?? 2)?.arity ?? 0;
  return 0;
}

/** 节点展示名：算子用服务端 label，字段用中文名，其余用字面量 */
export function nodeTitle(catalog: Catalog, node: CanvasNode): string {
  switch (node.kind) {
    case 'op':
      return findOp(catalog, node.op)?.label ?? node.op ?? '未知算子';
    case 'infix':
      return findInfix(catalog, node.op, node.arity ?? 2)?.label ?? node.op ?? '未知运算';
    case 'field':
      return findField(catalog, node.field)?.label ?? node.field ?? '未知字段';
    case 'constant':
      return String(node.value ?? 0);
    case 'output':
      return node.label ?? '因子输出';
    default:
      return '节点';
  }
}

/**
 * 标量参数缺省值的**唯一**兜底口径。
 *
 * 服务端如实报告窗口参数 required 且无默认（它不替调用方编数字），
 * 画布必须自己给一个，且只能有一处定义 —— 否则节点工厂给 5、
 * 编译器给 1，同一个积木会因来源不同编译出不同表达式。
 */
export function paramFallback(param: OpParam): number {
  return param.default ?? (param.type === 'window' ? DEFAULT_WINDOW : 1);
}

/** 参数默认值：窗口用画布缺省，其余优先服务端默认 */
export function defaultParams(op: OpDef): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of op.params) {
    out[p.name] = paramFallback(p);
  }
  return out;
}

export function paramLabel(p: OpParam): string {
  return p.type === 'window' ? '窗口长度' : '数值';
}

/** 按 category 分组，供左侧面板渲染 */
export function groupOps(catalog: Catalog): { category: string; label: string; items: OpDef[] }[] {
  const labels: Record<string, string> = {
    TS: '时序算子',
    CS: '截面算子',
    EL: '逐元素算子',
  };
  const order = ['TS', 'CS', 'EL'];
  return order
    .map((category) => ({
      category,
      label: labels[category] ?? category,
      // 不可自省签名的算子不进面板：放进去也只能生成坏表达式
      items: catalog.ops.filter((o) => o.category === category && isSupported(o)),
    }))
    .filter((g) => g.items.length > 0);
}
