'use client';

/**
 * 行业多角度分析报告渲染。
 *
 * 版式与个股分析共用 `ReportView`；本组件补上行业特有的两块：
 * 「行业概览」（成员数 / 全行业排名 / 龙头股）与空态措辞。
 */
import { Panel, Stat } from '@/components/Panel';
import ReportView from '@/components/ReportView';
import { isIndustryAnalysis, type IndustryAnalysis } from '@/lib/industry';
import { pctText } from '@/lib/report';

function OverviewPanel({ o }: { o: IndustryAnalysis['overview'] }) {
  return (
    <Panel
      title="行业概览"
      meta={`${o.industry_code} · 标准 ${o.std ?? '—'} · 基准 ${o.benchmark ?? '—'}`}
    >
      <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
        <Stat label="成分股" value={`${o.member_count}`} hint="按生效日 PIT 归属统计" />
        <Stat label="当日有行情" value={o.active_members == null ? '—' : `${o.active_members}`} />
        <Stat
          label="当日等权涨跌"
          value={o.day_ret == null ? '—' : `${o.day_ret >= 0 ? '+' : ''}${o.day_ret.toFixed(2)}%`}
        />
        <Stat
          label="全行业 20 日排名"
          value={o.rank ? `${o.rank.rank}/${o.rank.n_industries}` : '—'}
          hint={o.rank ? `${o.rank.percentile.toFixed(0)}% 分位` : '横截面数据不足'}
        />
      </div>

      {o.leaders.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-xs text-ink-faint">近 20 日区间涨幅最大的成员</div>
          <div className="grid grid-cols-1 gap-x-8 sm:grid-cols-2">
            {o.leaders.map((l) => (
              <div key={l.symbol}
                   className="flex items-baseline justify-between gap-3 border-b border-line py-1.5">
                <span className="truncate text-xs text-ink-dim">
                  {l.name ?? l.symbol}
                  <span className="ml-1 font-mono text-[11px] text-ink-faint">{l.symbol}</span>
                </span>
                <span className={`shrink-0 text-sm tabular-nums ${
                  (l.ret20 ?? 0) >= 0 ? 'text-up' : 'text-down'
                }`}>
                  {l.ret20 == null ? '—' : `${l.ret20 >= 0 ? '+' : ''}${l.ret20.toFixed(2)}%`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {o.notes?.length ? (
        <ul className="mt-3 space-y-1 text-[11px] text-ink-faint">
          {o.notes.map((n, i) => <li key={i}>· {n}</li>)}
        </ul>
      ) : null}

      <p className="mt-3 border-l-2 border-line-strong pl-3 text-[11px] leading-relaxed text-ink-faint">
        行业指数为成分股**等权合成**，不是交易所或申万官方指数；与行情软件的
        行业指数涨跌幅存在口径差异。成分股按观察日已生效的行业分类确定。
      </p>
    </Panel>
  );
}

export default function IndustryAnalysisView({ report }: { report: unknown }) {
  const valid = isIndustryAnalysis(report) ? report : null;
  return (
    <ReportView
      report={valid}
      emptyText="行业分析报告为空或结构不匹配 —— 请先同步行业分类与日线数据"
      meta={valid
        ? `观察日 ${valid.asof} · 契约 v${valid.schema_version} · 分析面覆盖 ${pctText(valid.score.angle_coverage)}`
        : undefined}
      extra={valid ? <OverviewPanel o={valid.overview} /> : null}
    />
  );
}
