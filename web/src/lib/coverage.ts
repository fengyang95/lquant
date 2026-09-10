/**
 * 覆盖度按月缺口加工（对接 GET /api/data/coverage/monthly，裸返回 {"rows":[...]}）。
 * 纯函数抽离：环比标橙逻辑在此，组件只负责渲染（ECharts 难进 jsdom，逻辑单测测这里）。
 */

export type MonthlyCoverageRow = {
  month: string; // "2024-01"
  avg_symbols: number;
  days: number;
};

/** 后端契约：裸 dict（非封套、非裸数组） */
export type MonthlyCoverageResp = { rows: MonthlyCoverageRow[] };

/** 环比跌幅超过该比例的月份视为疑似缺口月 */
export const COVERAGE_DROP_THRESHOLD = 0.3;

/** 缺口月柱色 —— 数据完整度警示用项目金色 token，不用涨跌红绿 */
export const COVERAGE_FLAG_COLOR = '#B08A3E';
/** 正常月份柱色 —— 中性靛 */
export const COVERAGE_BASE_COLOR = '#31589E';

export type FlaggedMonthlyRow = MonthlyCoverageRow & {
  /** 环比跌幅是否超阈值 */
  drop: boolean;
  /** 环比跌幅（正数，如 0.35 表示较上月跌 35%）；首月或上月为 0 时无环比，null */
  drop_pct: number | null;
};

/** 环比跌幅标橙：对上月 avg_symbols 跌幅 > threshold 的月打 drop 标记。
 *  首月 / 上月为 0（无法算环比）→ drop=false, drop_pct=null。不可变：返回新数组。 */
export function flagMonthlyCoverage(
  rows: MonthlyCoverageRow[],
  threshold: number = COVERAGE_DROP_THRESHOLD,
): FlaggedMonthlyRow[] {
  return rows.map((r, i) => {
    if (i === 0) return { ...r, drop: false, drop_pct: null };
    const prev = rows[i - 1].avg_symbols;
    if (!prev) return { ...r, drop: false, drop_pct: null };
    const dropPct = (prev - r.avg_symbols) / prev;
    return { ...r, drop: dropPct > threshold, drop_pct: dropPct };
  });
}

/** ECharts 柱状图 option：x=month，y=avg_symbols，缺口月金色柱 */
export function monthlyCoverageOption(rows: FlaggedMonthlyRow[]): object {
  return {
    grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
    tooltip: {
      trigger: 'axis',
      formatter: (params: unknown) => {
        const p = (Array.isArray(params) ? params[0] : params) as {
          name: string; value: number; dataIndex: number;
        };
        const r = rows[p.dataIndex];
        const n = typeof p.value === 'number' ? p.value.toLocaleString() : '—';
        const pct = r?.drop_pct != null ? `，环比 ${(r.drop_pct * 100).toFixed(1)}%` : '';
        const warn = r?.drop ? ' · 疑似缺口' : '';
        return `${p.name}：平均 ${n} 只/日${pct}${warn}（${r?.days ?? '—'} 个交易日）`;
      },
    },
    xAxis: {
      type: 'category',
      data: rows.map((r) => r.month),
      axisLine: { lineStyle: { color: '#E3E3DC' } },
      axisTick: { show: false },
      axisLabel: { color: '#94989F', fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      splitLine: { lineStyle: { color: '#E3E3DC' } },
      axisLabel: { color: '#94989F', fontSize: 10 },
    },
    series: [
      {
        type: 'bar',
        data: rows.map((r) => ({
          value: r.avg_symbols,
          itemStyle: { color: r.drop ? COVERAGE_FLAG_COLOR : COVERAGE_BASE_COLOR },
        })),
        barMaxWidth: 18,
      },
    ],
  };
}

/** 默认统计窗口：近 N 个月（含当月），返回 [start, end]（YYYY-MM-DD，UTC 口径避免时区跨月漂移） */
export function lastNMonthsRange(n: number, today: Date = new Date()): [string, string] {
  const end = today.toISOString().slice(0, 10);
  const start = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - n, 1));
  return [start.toISOString().slice(0, 10), end];
}
