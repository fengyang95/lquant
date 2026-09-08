/**
 * ECharts 主题常量 —— 「研报台」：墨色文字、发丝网格、靛/朱/青/金系列色。
 * 各页面图表只写差异项，公共坐标轴样式从这里取。
 */

export const C = {
  up: '#C3352B',
  down: '#1E7C55',
  ink: '#22252B',
  inkDim: '#5C6169',
  inkFaint: '#94989F',
  indigo: '#31589E',
  gold: '#B08A3E',
  line: '#E3E3DC',
} as const;

/** 多系列折线用色（靛为首，朱/青留给涨跌语义） */
export const SERIES_COLORS = [C.indigo, C.gold, '#6B4F9E', '#3E8E7E', '#A85B4B', '#4E6E8E'];

/** 公共坐标轴样式 */
export function axes(xExtra: object = {}, yExtra: object = {}) {
  return {
    xAxis: {
      type: 'category' as const,
      axisLine: { lineStyle: { color: C.line } },
      axisTick: { show: false },
      axisLabel: { color: C.inkDim, fontSize: 10 },
      ...xExtra,
    },
    yAxis: {
      type: 'value' as const,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: C.line } },
      axisLabel: { color: C.inkDim, fontSize: 10 },
      ...yExtra,
    },
  };
}

/** 公共 legend 样式 */
export function legend(extra: object = {}) {
  return {
    textStyle: { color: C.inkDim, fontSize: 11 },
    itemWidth: 14,
    itemHeight: 2,
    itemGap: 12,
    ...extra,
  };
}

export const tooltip = {
  trigger: 'axis' as const,
  backgroundColor: '#FBFBF9',
  borderColor: '#CDCDC4',
  borderWidth: 1,
  textStyle: { color: C.ink, fontSize: 12 },
  extraCssText: 'box-shadow: 0 2px 12px rgba(34,37,43,.08);',
};
