'use client';

import { useEffect, useRef } from 'react';
import { createChart, ColorType, LineStyle } from 'lightweight-charts';

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

/** K 线 + 成交量 + 均线叠加（lightweight-charts）。A 股配色：阳线红、阴线绿。 */
export default function KChart({ bars, overlays = [], height = 380 }:
{ bars: Bar[]; overlays?: Overlay[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

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

    chart.timeScale().fitContent();

    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [bars, overlays, height]);

  if (!bars.length) {
    return (
      <div className="border border-dashed border-line-strong bg-panel py-16 text-center text-sm text-ink-faint">
        暂无日线数据 —— 先跑 <code className="mx-1 bg-paper px-1">lq data demo</code>
      </div>
    );
  }
  return <div ref={ref} className="w-full" style={{ height }} />;
}
