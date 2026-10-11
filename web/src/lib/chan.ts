/**
 * 缠论（日线）—— 类型契约与几何纯函数。
 *
 * 与后端 `GET /security/{symbol}/chan` 的响应一一对应。几何换算抽在这里、
 * 与 canvas 绘制分离，是为了**能不依赖浏览器就单测**（见 __tests__/chan.test.ts）：
 * 画错一笔的代价是用户看错结构，比崩溃更难发现。
 */

export type ChanFx = { dt: string; mark: 'G' | 'D'; value: number };

export type ChanBi = {
  sdt: string;
  edt: string;
  direction: 'up' | 'down';
  high: number;
  low: number;
  n_bars: number;
};

export type ChanZs = {
  sdt: string;
  edt: string;
  zg: number;
  zd: number;
  gg: number;
  dd: number;
  n_bis: number;
};

export type ChanEvent = { dt: string; kind: string; label: string; detail: string };

export type ChanSignalKind =
  | 'beichi' | 'buy1' | 'sell1' | 'buy2' | 'sell2' | 'buy3' | 'sell3';

export type ChanData = {
  symbol: string;
  freq: string;
  available: boolean;
  /** available=false 时给出面向用户的原因 */
  reason?: string;
  asof?: string | null;
  n_bars?: number;
  fx?: ChanFx[];
  bi?: ChanBi[];
  zs?: ChanZs[];
  signals?: { latest: Partial<Record<ChanSignalKind, string | null>>; events: ChanEvent[] };
  notes?: string[];
};

/** 信号种类的展示顺序（面板按此排列徽标）。 */
export const SIGNAL_ORDER: ChanSignalKind[] = [
  'beichi', 'buy1', 'buy2', 'buy3', 'sell1', 'sell2', 'sell3',
];

export const SIGNAL_LABELS: Record<ChanSignalKind, string> = {
  beichi: '背驰', buy1: '一买', buy2: '二买', buy3: '三买',
  sell1: '一卖', sell2: '二卖', sell3: '三卖',
};

/** 买点用涨色（A 股红）、卖点用跌色、背驰用金色 —— 与 K 线配色同一套语义。 */
export function signalTone(kind: string): 'up' | 'down' | 'neutral' {
  if (kind.startsWith('buy')) return 'up';
  if (kind.startsWith('sell')) return 'down';
  return 'neutral';
}

export function signalColor(kind: string): string {
  const tone = signalTone(kind);
  if (tone === 'up') return '#C3352B';
  if (tone === 'down') return '#1E7C55';
  return '#B08A3E';
}

/**
 * 一笔的两个端点价格。
 *
 * 后端只给 `high`/`low`（= 两端分型的极值），方向决定谁在起点：
 * 向上笔从低点走到高点，向下笔反之。
 */
export function biEndpoints(bi: ChanBi): { sPrice: number; ePrice: number } {
  return bi.direction === 'up'
    ? { sPrice: bi.low, ePrice: bi.high }
    : { sPrice: bi.high, ePrice: bi.low };
}

/**
 * 把矩形的两个 x 夹到可见宽度内。
 *
 * 中枢常常比视口宽（一个中枢可横跨数百根 K 线），若任一端在视口外
 * `timeToCoordinate` 会返回 null；调用方只需把可见的那端与视口边界拼起来。
 * 两端都不可见时返回 null（整体不画）。
 */
export function clampSpan(
  x1: number | null,
  x2: number | null,
  width: number,
): { x1: number; x2: number } | null {
  if (x1 == null && x2 == null) return null;
  const a = x1 == null ? 0 : x1;
  const b = x2 == null ? width : x2;
  return a <= b ? { x1: a, x2: b } : { x1: b, x2: a };
}

/**
 * 分型标记的三角形三点坐标。
 *
 * 尖端**贴着极值点**、底边朝外，这样标记整体落在 K 线之外（顶分型在上、底分型在下），
 * 不会压住实体。返回顺序为 ``[尖端, 左下, 右下]``。
 */
export function fxTriangle(
  x: number,
  y: number,
  mark: 'G' | 'D',
  size = 5,
): [number, number][] {
  const dir = mark === 'G' ? -1 : 1; // G 画在价格上方、D 画在下方
  const baseY = y + dir * 2 * size;
  return [[x, y], [x - size, baseY], [x + size, baseY]];
}

/** 事件点相对 K 线的高低点偏移：买点画在下方、卖点画在上方，避免压住实体。 */
export function eventAnchor(
  kind: string,
  bar: { high: number; low: number },
): number {
  return kind.startsWith('sell') ? bar.high : bar.low;
}
