/** 纯格式化工具 —— 无 JSX/DOM 依赖，可被 vitest 直接单测。
 *
 * A 股约定：涨红跌绿（text-up / text-down / text-flat 在 globals.css）。
 */

/** 涨跌幅 → "+1.23%"（负数自带负号） */
export function fmtPct(v?: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return `${sign}${(v * 100).toFixed(digits)}%`;
}

/** 涨跌对应的颜色类（涨红跌绿） */
export function pctColorClass(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return 'text-flat';
  return v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-flat';
}

/** 元 → 亿元展示；≥1 万亿自动换档 */
export function fmtYi(v?: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—';
  const yi = v / 1e8;
  return yi >= 10000 ? `${(yi / 10000).toFixed(2)}万亿` : `${yi.toFixed(digits)}亿`;
}

export function fmtNum(v?: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(digits);
}
