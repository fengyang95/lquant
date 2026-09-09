/**
 * 回测详情页纯函数辅助 —— record() 自定义曲线 + 自定义分析(chart)选项构建。
 * 与 React 解耦，便于 vitest 单测。
 */

import { C, SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';

export type RecordPt = { date: string; value: number };
export type RecordsMap = { [key: string]: RecordPt[] };

export type CustomChartItem = {
  type: 'chart';
  title?: string;
  data: Record<string, unknown>[];
  x: string;
  ys: string[];
};
export type CustomTableItem = {
  type: 'table';
  title?: string;
  columns: string[];
  rows: unknown[][];
};
export type CustomAnalysisItem = CustomChartItem | CustomTableItem;

/** record() 单条曲线 → ECharts 折线 option（空数据返回 null） */
export function recordChartOption(key: string, pts: RecordPt[] | undefined) {
  if (!pts?.length) return null;
  return {
    tooltip,
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    ...axes({ data: pts.map((p) => p.date) }),
    series: [
      {
        name: key,
        type: 'line' as const,
        data: pts.map((p) => (Number.isFinite(p.value) ? +p.value.toFixed(6) : null)),
        showSymbol: false,
        lineStyle: { width: 1.4, color: C.indigo },
        itemStyle: { color: C.indigo },
      },
    ],
  };
}

/** 自定义分析 chart 项 → ECharts 多系列折线 option（x/ys 字段映射，空数据返回 null） */
export function customChartOption(item: CustomChartItem) {
  if (!item?.data?.length || !item.x || !item.ys?.length) return null;
  const xs = item.data.map((r) => String(r[item.x] ?? ''));
  return {
    tooltip,
    legend: legend({ data: item.ys, top: 0 }),
    grid: { left: 60, right: 20, top: 30, bottom: 30 },
    ...axes({ data: xs }),
    series: item.ys.map((y, i) => ({
      name: y,
      type: 'line' as const,
      data: item.data.map((r) => {
        const v = r[y];
        return typeof v === 'number' && Number.isFinite(v) ? v : null;
      }),
      showSymbol: false,
      lineStyle: { width: 1.3, color: SERIES_COLORS[i % SERIES_COLORS.length] },
      itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    })),
  };
}
