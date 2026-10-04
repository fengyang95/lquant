/**
 * 因子编辑画布的类型契约。
 *
 * 原则：画布**不持有算子语义**。算子、参数、中文说明全部来自服务端
 * `GET /factors/ops`（见 catalog.ts），这里只定义"长什么样"。
 */

/** 算子标量参数（服务端自省函数签名得到） */
export type OpParam = {
  name: string;
  type: 'window' | 'number';
  required: boolean;
  default: number | null;
};

/** 函数式算子（OPS 注册表） */
export type OpDef = {
  name: string;
  category: 'TS' | 'CS' | 'EL';
  /** 中文语义，**原样展示**——语义分歧以服务端为准 */
  label: string;
  min_window: number;
  /** 序列输入端口数 */
  series_arity: number;
  params: OpParam[];
};

/** 语法级中缀算子（parser 内建：`+ - * / < >`） */
export type InfixDef = {
  token: string;
  label: string;
  arity: number;
};

export type FieldDef = {
  name: string;
  label: string;
};

export type Catalog = {
  ops: OpDef[];
  infix: InfixDef[];
  fields: FieldDef[];
};

export type NodeKind = 'field' | 'constant' | 'op' | 'infix' | 'output';

export type CanvasNode = {
  id: string;
  kind: NodeKind;
  /** kind=op：算子名（须存在于 catalog.ops）；kind=infix：中缀 token */
  op?: string;
  /**
   * kind=infix：1（一元，如取相反数）| 2（二元）。缺省按 2。
   * `-` 同时是一元取反与二元减法，靠 arity 区分（服务端同样这么声明）。
   */
  arity?: number;
  /** kind=field：字段名 */
  field?: string;
  /** kind=constant：数值 */
  value?: number;
  /** kind=op：参数值，键为 OpParam.name */
  params?: Record<string, number>;
  /** kind=output：因子名（仅用于展示） */
  label?: string;
  /** 画布坐标（React Flow 用；纯逻辑层不关心） */
  x?: number;
  y?: number;
};

export type CanvasEdge = {
  id: string;
  source: string;
  target: string;
  /** 下游节点的第几个序列输入端口（0-based，对应 DSL 位置参数顺序） */
  targetPort: number;
};

export type CompileResult = {
  expression: string;
  warnings: string[];
};

/** 服务端 AST 节点（POST /factors/ast） */
export type AstNode =
  | { kind: 'field'; name: string }
  | { kind: 'number'; value: number }
  | { kind: 'unary'; op: string; arg: AstNode }
  | { kind: 'binary'; op: string; left: AstNode; right: AstNode }
  | { kind: 'call'; name: string; args: AstNode[] };

export type AstResponse = {
  /** 服务端归一后的表达式（历史 qlib 写法会在此变成 lquant DSL） */
  expression: string;
  ast: AstNode;
  /** 服务端是否把历史 qlib 写法翻译成了 lquant DSL */
  translated?: boolean;
};

/** 打开已有因子 / 粘贴表达式的结果 */
export type LoadExpressionResult = {
  /** 画布映射告警（有损映射时禁止保存） */
  warnings: string[];
  /** 是否发生过 qlib → DSL 兼容翻译 */
  translated: boolean;
};

/** 窗口控件缺省值：服务端如实报告 required 但无默认，UI 给一个常识值 */
export const DEFAULT_WINDOW = 5;
