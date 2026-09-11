/** 回测工作台纯逻辑 —— dirty 判定 / run-code payload 构建 / 参数解析。与 React 解耦便于单测。 */

export type EditorParams = { start: string; end: string; formulas: string };

export type StrategyMeta = {
  id?: string;
  name: string;
  source: 'builtin' | 'user';
  description?: string;
  version?: number;
  updated_at?: string;
};

export type RunPayload = {
  code: string;
  start: string;
  end?: string;
  factor_formulas: string[];
  strategy_id?: string;
};

export type Snapshot = {
  name: string;
  description: string;
  code: string;
  params: EditorParams;
};

/** 逗号分隔的因子公式串 → trim 后去空的数组。 */
export function parseFormulas(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '');
}

/** 构建 run-code 请求体：end 为空串时省略 end；strategyId 为 null/'' 时省略 strategy_id。 */
export function buildRunPayload(
  code: string,
  params: EditorParams,
  strategyId: string | null,
): RunPayload {
  const payload: RunPayload = {
    code,
    start: params.start,
    factor_formulas: parseFormulas(params.formulas),
  };
  const withEnd = params.end !== '' ? { ...payload, end: params.end } : payload;
  return strategyId ? { ...withEnd, strategy_id: strategyId } : withEnd;
}

/** 编辑快照与基准快照对比：不同则视为 dirty。base 为 null 时，全空视为未修改。 */
export function isDirty(cur: Snapshot, base: Snapshot | null): boolean {
  if (base === null) {
    const allEmpty =
      cur.name === '' &&
      cur.description === '' &&
      cur.code === '' &&
      cur.params.start === '' &&
      cur.params.end === '' &&
      cur.params.formulas === '';
    return !allEmpty;
  }
  return (
    cur.name !== base.name ||
    cur.description !== base.description ||
    cur.code !== base.code ||
    cur.params.start !== base.params.start ||
    cur.params.end !== base.params.end ||
    cur.params.formulas !== base.params.formulas
  );
}
