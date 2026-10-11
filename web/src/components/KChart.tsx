'use client';

import { useEffect, useRef } from 'react';
import { createChart, ColorType, LineStyle, type Time } from 'lightweight-charts';
import {
  biEndpoints,
  clampSpan,
  eventAnchor,
  fxTriangle,
  signalColor,
  type ChanData,
} from '@/lib/chan';

export type Bar = {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
};

/** 叠加线（如 MA5/MA20/MA60）。data 与 bars 等长，null 为缺口。 */
export type Overlay = {
  name: string;
  color: string;
  data: (number | null)[];
};

/**
 * K 线 + 成交量 + 均线叠加 + 缠论结构（lightweight-charts）。A 股配色：阳线红、阴线绿。
 *
 * 缠论是**分段结构**（笔是折线、中枢是矩形、分型/买卖点是散点），而
 * lightweight-charts v4 没有自定义 series 插件 API —— 所以走「自绘 canvas 叠加层」：
 * 在图表容器上再压一层同尺寸 canvas，用 `timeToCoordinate` / `priceToCoordinate`
 * 把数据坐标换成像素坐标后手绘。好处是与主图共用同一套时间轴与价格轴，
 * 缩放平移天然对齐。
 *
 * 不传 `chan` 时行为与加缠论之前完全一致（叠加层根本不创建）。
 */
export default function KChart({ bars, overlays = [], chan, height = 380 }:
{ bars: Bar[]; overlays?: Overlay[]; chan?: ChanData; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chanCanvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !bars.length) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#5C6169',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#ECECE6' },
        horzLines: { color: '#ECECE6' },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: false },
    });

    const candle = chart.addCandlestickSeries({
      upColor: '#C3352B',
      downColor: '#1E7C55',
      borderVisible: false,
      wickUpColor: '#C3352B',
      wickDownColor: '#1E7C55',
    });
    candle.setData(
      bars.map((b) => ({
        time: b.trade_date,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );

    // 均线叠加
    for (const ov of overlays) {
      const points = bars
        .map((b, i) => ({ time: b.trade_date, value: ov.data[i] }))
        .filter((p) => p.value != null);
      if (!points.length) continue;
      const line = chart.addLineSeries({
        color: ov.color,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      line.setData(points);
    }

    if (bars.some((b) => b.volume != null)) {
      const vol = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: '',
      });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vol.setData(
        bars.map((b) => ({
          time: b.trade_date,
          value: b.volume ?? 0,
          color: b.close >= b.open ? 'rgba(195,53,43,0.35)' : 'rgba(30,124,85,0.35)',
        })),
      );
    }

    const timeScale = chart.timeScale();

    // ---- 缠论叠加层（自绘 canvas）----
    // 中枢常比视口宽，因此每次可见区间变化都要重绘：坐标是「当前视口」的函数。
    const drawChan = () => {
      const cv = chanCanvasRef.current;
      if (!cv || !chan) return;
      const ctx = cv.getContext('2d');
      if (!ctx) return;
      const w = el.clientWidth;
      const dpr = window.devicePixelRatio || 1;
      cv.width = Math.max(1, Math.round(w * dpr));
      cv.height = Math.max(1, Math.round(height * dpr));
      cv.style.width = `${w}px`;
      cv.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, height);

      const X = (d: string) => timeScale.timeToCoordinate(d as Time);
      const Y = (p: number) => candle.priceToCoordinate(p);

      // 中枢：半透明区间 + 上下沿描边；gg/dd 用虚线标延伸极值
      for (const z of chan.zs ?? []) {
        const span = clampSpan(X(z.sdt), X(z.edt), w);
        if (!span) continue;
        const yTop = Y(z.zg);
        const yBot = Y(z.zd);
        if (yTop == null || yBot == null) continue;
        const top = Math.min(yTop, yBot);
        const hgt = Math.abs(yBot - yTop);
        ctx.fillStyle = 'rgba(49,88,158,0.10)';
        ctx.fillRect(span.x1, top, span.x2 - span.x1, hgt);
        ctx.strokeStyle = 'rgba(49,88,158,0.55)';
        ctx.lineWidth = 1;
        ctx.strokeRect(span.x1 + 0.5, top + 0.5, Math.max(0, span.x2 - span.x1 - 1), hgt);
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(49,88,158,0.35)';
        for (const p of [z.gg, z.dd]) {
          const y = Y(p);
          if (y == null) continue;
          ctx.beginPath();
          ctx.moveTo(span.x1, y);
          ctx.lineTo(span.x2, y);
          ctx.stroke();
        }
        ctx.restore();
      }

      // 笔：相邻分型端点连折线，方向决定颜色（与 K 线同一套涨跌语义）
      ctx.lineWidth = 1.5;
      for (const b of chan.bi ?? []) {
        const x1 = X(b.sdt);
        const x2 = X(b.edt);
        const { sPrice, ePrice } = biEndpoints(b);
        const y1 = Y(sPrice);
        const y2 = Y(ePrice);
        if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
        ctx.strokeStyle = b.direction === 'up' ? '#C3352B' : '#1E7C55';
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }

      // 分型：顶分型在价格上方画倒三角，底分型反之
      for (const f of chan.fx ?? []) {
        const x = X(f.dt);
        const y = Y(f.value);
        if (x == null || y == null) continue;
        const tri = fxTriangle(x, y, f.mark);
        ctx.fillStyle = f.mark === 'G' ? '#C3352B' : '#1E7C55';
        ctx.beginPath();
        ctx.moveTo(tri[0][0], tri[0][1]);
        ctx.lineTo(tri[1][0], tri[1][1]);
        ctx.lineTo(tri[2][0], tri[2][1]);
        ctx.closePath();
        ctx.fill();
      }

      // 信号事件：买点画在当根低点下方、卖点画在高点上方，避免压住实体
      const byDate = new Map(bars.map((b) => [b.trade_date, b]));
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      for (const e of chan.signals?.events ?? []) {
        const bar = byDate.get(e.dt);
        const x = X(e.dt);
        if (!bar || x == null) continue;
        const y = Y(eventAnchor(e.kind, bar));
        if (y == null) continue;
        const sell = e.kind.startsWith('sell');
        ctx.fillStyle = signalColor(e.kind);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillText(e.label, x, sell ? y - 7 : y + 13);
      }
    };

    let rangeHandler: (() => void) | null = null;
    if (chan) {
      const cv = document.createElement('canvas');
      cv.style.position = 'absolute';
      cv.style.inset = '0';
      cv.style.pointerEvents = 'none';
      cv.style.zIndex = '10';
      el.style.position = 'relative';
      el.appendChild(cv);
      chanCanvasRef.current = cv;
      rangeHandler = drawChan;
      timeScale.subscribeVisibleLogicalRangeChange(rangeHandler);
    }

    timeScale.fitContent();
    drawChan();

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth });
      drawChan();
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      if (rangeHandler) timeScale.unsubscribeVisibleLogicalRangeChange(rangeHandler);
      chanCanvasRef.current?.remove();
      chanCanvasRef.current = null;
      chart.remove();
    };
  }, [bars, overlays, chan, height]);

  if (!bars.length) {
    return (
      <div className="border border-dashed border-line-strong bg-panel py-16 text-center text-sm text-ink-faint">
        暂无日线数据 —— 先跑 <code className="mx-1 bg-paper px-1">lq data demo</code>
      </div>
    );
  }
  return <div ref={ref} className="w-full" style={{ height }} />;
}
