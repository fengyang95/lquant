'use client';

import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote } from '@/components/States';
import { fetcher } from '@/lib/api';
import {
  flagMonthlyCoverage, lastNMonthsRange, monthlyCoverageOption,
  type MonthlyCoverageResp,
} from '@/lib/coverage';
import Chart from '@/components/Chart';

/** 覆盖度按月缺口图：每月平均标的数柱状图，环比跌幅 >30% 的月份标金。
 *  数据完整度警示（金），非涨跌语义（不用红绿）。后端裸返回 {"rows":[...]}。 */
export default function CoverageMonthlyChart() {
  const [start, end] = lastNMonthsRange(24);
  const { data, error, isLoading } = useSWR<MonthlyCoverageResp>(
    `/data/coverage/monthly?start=${start}&end=${end}`,
    fetcher,
  );

  const rows = flagMonthlyCoverage(data?.rows ?? []);
  const option = rows.length ? monthlyCoverageOption(rows) : null;
  const flagged = rows.filter((r) => r.drop);

  return (
    <Panel
      title="覆盖度 · 按月"
      meta={`近 24 个月 · 平均标的数/日${flagged.length ? ` · ${flagged.length} 个疑似缺口月` : ''}`}
      actions={
        flagged.length > 0 && (
          <span className="text-xs text-gold">
            <span className="mr-1 inline-block h-2 w-2 rounded-[2px] bg-gold align-middle" />
            环比跌幅 &gt;30%（疑似缺口）
          </span>
        )
      }
    >
      {error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-6 text-sm text-ink-faint">加载中…</p>
      ) : !rows.length ? (
        <Empty>暂无月度覆盖数据 —— 数据湖为空</Empty>
      ) : (
        <Chart option={option} height={220} />
      )}
    </Panel>
  );
}
