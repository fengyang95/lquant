'use client';

import ReactECharts from 'echarts-for-react';

/** 统一图表容器：墨色文字主题 + 无动画干扰（notMerge + lazyUpdate） */
export default function Chart({
  option,
  height = 260,
  onClick,
}: {
  option: object | null;
  height?: number;
  onClick?: (params: unknown) => void;
}) {
  if (!option) return null;
  return (
    <ReactECharts
      option={{
        animation: false,
        textStyle: { fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif' },
        ...option,
      }}
      style={{ height }}
      notMerge
      lazyUpdate
      onEvents={onClick ? { click: onClick } : undefined}
    />
  );
}
