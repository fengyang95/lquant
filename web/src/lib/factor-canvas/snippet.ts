/**
 * 目录算子 → 可直接插入的 DSL 片段。
 *
 * 给「因子注册 / 编辑表单」的算子面板用：点一下就往表达式里插一段**合法**
 * 的调用。原先那里硬编码了 `Mean(` `Ref(` `Delta(` `Ratio(` —— 这些名字
 * lquant DSL 根本不认（正确的是 Ts_Mean / Ts_Delay，且没有 Delta / Ratio），
 * 点出来的表达式过不了校验。改成从目录生成，就不可能再和引擎对不上。
 */
import { isSupported, paramFallback } from './catalog';
import { formatNumber } from './compile';
import type { Catalog, InfixDef, OpDef } from './types';

/** 序列实参占位：统一用收盘价，用户自己改 */
const SERIES_PLACEHOLDER = '$close';

export function opSnippet(op: OpDef): string {
  const args = Array.from({ length: op.series_arity }, () => SERIES_PLACEHOLDER);
  for (const param of op.params) {
    args.push(formatNumber(paramFallback(param)));
  }
  return `${op.name}(${args.join(',')})`;
}

export function infixSnippet(def: InfixDef): string {
  if (def.arity === 1) return `-(${SERIES_PLACEHOLDER})`;
  return `(${SERIES_PLACEHOLDER} ${def.token} $open)`;
}

export type SnippetGroup = {
  label: string;
  items: { key: string; label: string; insert: string; hint: string }[];
};

/** 面板分组：字段 / 四则与比较 / TS / CS / EL */
export function snippetGroups(catalog: Catalog): SnippetGroup[] {
  const groups: SnippetGroup[] = [];

  if (catalog.fields.length > 0) {
    groups.push({
      label: '数据字段',
      items: catalog.fields.map((field) => ({
        key: `field-${field.name}`,
        label: field.label,
        insert: `$${field.name}`,
        hint: `$${field.name}`,
      })),
    });
  }

  if (catalog.infix.length > 0) {
    groups.push({
      label: '四则与比较',
      items: catalog.infix.map((def) => ({
        key: `infix-${def.token}-${def.arity}`,
        label: def.label,
        insert: infixSnippet(def),
        hint: def.token,
      })),
    });
  }

  const categoryLabel: Record<string, string> = { TS: '时序算子', CS: '截面算子', EL: '逐元素算子' };
  for (const category of ['TS', 'CS', 'EL']) {
    const ops = catalog.ops.filter((op) => op.category === category && isSupported(op));
    if (ops.length === 0) continue;
    groups.push({
      label: categoryLabel[category] ?? category,
      items: ops.map((op) => ({
        key: `op-${op.name}`,
        label: op.label,
        insert: opSnippet(op),
        hint: op.name,
      })),
    });
  }

  return groups;
}
