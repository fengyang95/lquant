'use client';

/**
 * 多角度分析报告的**通用渲染器**（纯展示，无取数逻辑）。
 *
 * 个股分析（`SecurityAnalysis`）与行业分析（`IndustryAnalysis`）共用同一套
 * 版式与配色 —— 同一份报告契约在两处渲染出不同样式，会让读者误以为
 * 「分数口径不一样」。各域只需通过 `extra` 插入自己的专属区块。
 *
 * 展示纪律与后端一致：**缺数据就显示「缺失 + 怎么补」，不显示 0 或占位分**。
 * 未参与评分的角度会明确标注「不评分」，避免读者把中性当成结论。
 */
import type { ReactNode } from 'react';
import { Panel, Stat } from '@/components/Panel';
import { Empty } from '@/components/States';
import {
  isAngleReport,
  pctText,
  scoreWidth,
  signalBg,
  signalLabel,
  signalTone,
  type AngleResult,
  type Metric,
  type RiskBlock,
  type ScoreBlock,
} from '@/lib/report';

export type AngleReportShape = {
  schema_version: string;
  asof: string;
  score: ScoreBlock;
  angles: AngleResult[];
  verdict: { points: string[]; risks: string[] };
  risk: RiskBlock;
  disclaimer: string;
};

export function StanceBadge({ signal }: { signal?: AngleResult['stance'] }) {
  return (
    <span className={`px-1.5 py-0.5 text-[11px] leading-4 ${signalBg(signal)}`}>
      {signalLabel(signal)}
    </span>
  );
}

/** 一行指标：标签 + 值 + 分位条 + 备注 */
export function MetricRow({ m }: { m: Metric }) {
  const tone = signalTone(m.signal);
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1.5 last:border-b-0">
      <div className="min-w-0">
        <div className="truncate text-xs text-ink-dim">{m.label}</div>
        {m.note ? (
          <div className="truncate text-[11px] text-ink-faint">{m.note}</div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {m.percentile != null && (
          <span
            className="hidden h-1 w-10 bg-line sm:block"
            title={`分位 ${m.percentile.toFixed(0)}%`}
          >
            <span
              className="block h-1 bg-ink-faint"
              style={{ width: `${Math.max(0, Math.min(100, m.percentile))}%` }}
            />
          </span>
        )}
        <span className={`text-sm tabular-nums ${tone}`}>
          {m.display ?? (m.value != null ? String(m.value) : '—')}
        </span>
      </div>
    </div>
  );
}

export function AngleCard({ a }: { a: AngleResult }) {
  const score = a.score;
  return (
    <div className="border border-line bg-panel">
      <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <div className="flex items-baseline gap-2">
          <h3 className="text-[13px] font-semibold text-ink">{a.label}</h3>
          <span className="text-[11px] text-ink-faint">
            权重 {(a.weight * 100).toFixed(0)}%
          </span>
        </div>
        <div className="flex items-center gap-2">
          {score != null ? (
            <span className={`font-song text-lg font-semibold tabular-nums ${signalTone(a.stance)}`}>
              {score.toFixed(0)}
            </span>
          ) : (
            <span className="text-xs text-ink-faint">不评分</span>
          )}
          <StanceBadge signal={a.stance} />
        </div>
      </header>

      {/* 分数条：让「50 是中性」一眼可见 */}
      {score != null && (
        <div className="h-1 w-full bg-line">
          <div className={`h-1 ${score >= 50 ? 'bg-up' : 'bg-down'}`} style={{ width: scoreWidth(score) }} />
        </div>
      )}

      <div className="px-3 py-2">
        <p className="text-xs leading-relaxed text-ink-dim">{a.summary}</p>

        {!a.available && a.hint ? (
          <p className="mt-2 border-l-2 border-line-strong pl-2 text-[11px] text-ink-faint">
            {a.hint}
          </p>
        ) : null}

        {a.metrics.length > 0 && (
          <div className="mt-2">
            {a.metrics.map((m) => <MetricRow key={m.key} m={m} />)}
          </div>
        )}

        {a.available && a.coverage < 0.5 && (
          <p className="mt-2 text-[11px] text-ink-faint">
            角度内数据覆盖 {pctText(a.coverage)}
          </p>
        )}
      </div>
    </div>
  );
}

type Props = {
  report: unknown;
  /** `overview` 面板标题下的一行说明（各域自己组织）。 */
  meta?: ReactNode;
  /** 空态提示（默认按「数据未同步」措辞）。 */
  emptyText?: ReactNode;
  /** 插入在综合评分之后、角度明细之前的专属区块。 */
  extra?: ReactNode;
};

export default function ReportView({ report, meta, emptyText, extra }: Props) {
  if (!isAngleReport(report)) {
    return <Empty>{emptyText ?? '分析报告为空或结构不匹配 —— 请先同步数据'}</Empty>;
  }
  const r = report as unknown as AngleReportShape;
  const { score } = r;
  const scored = r.angles.filter((a) => a.score != null);

  return (
    <div className="space-y-5">
      {/* 综合评分 */}
      <Panel title="综合评分"
             meta={meta ?? `观察日 ${r.asof} · 契约 v${r.schema_version}`}>
        <div className="flex flex-wrap items-center gap-x-10 gap-y-4">
          <div className="flex items-end gap-3">
            <span className={`font-song text-5xl font-semibold leading-none tabular-nums ${
              score.score == null ? 'text-ink-faint' : signalTone(score.stance)
            }`}>
              {score.score == null ? '—' : score.score.toFixed(0)}
            </span>
            <div className="pb-1">
              <div className="text-sm font-semibold text-ink">{score.grade}</div>
              <div className="mt-0.5"><StanceBadge signal={score.stance} /></div>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-x-8">
            <Stat label="有分角度" value={`${score.n_scored}/${score.n_angles}`} />
            <Stat label="分析面覆盖" value={pctText(score.angle_coverage)}
                  hint="有多少角度真的算出来了" />
            <Stat label="数据面覆盖" value={pctText(score.data_coverage)}
                  hint="已算角度的内部子项完整度" />
          </div>
        </div>

        {score.score == null && (
          <p className="mt-3 border-l-2 border-line-strong pl-3 text-xs text-ink-faint">
            可用数据不足以形成综合判断。下方每个角度都标注了缺失原因与补齐方式。
          </p>
        )}

        {/* 各角度贡献：让总分可解释 */}
        {score.contributions.length > 0 && (
          <div className="mt-4">
            <div className="mb-1.5 text-xs text-ink-faint">各角度对总分的贡献</div>
            <div className="space-y-1">
              {score.contributions.map((c) => {
                const w = Math.min(50, Math.abs(c.contribution) * 4);
                return (
                  <div key={c.id} className="flex items-center gap-2 text-xs">
                    <span className="w-24 shrink-0 text-ink-dim">{c.label}</span>
                    <span className="flex h-3 flex-1 items-center">
                      {/* 以中线为原点，向右红（拉高）向左绿（拉低） */}
                      <span className="relative flex h-3 w-full">
                        <span className="absolute left-1/2 top-0 h-3 w-px bg-line-strong" />
                        <span
                          className={`absolute top-0.5 h-2 ${c.contribution >= 0 ? 'bg-up' : 'bg-down'}`}
                          style={c.contribution >= 0
                            ? { left: '50%', width: `${w}%` }
                            : { right: '50%', width: `${w}%` }}
                        />
                      </span>
                    </span>
                    <span className="w-28 shrink-0 text-right tabular-nums text-ink-faint">
                      {c.score.toFixed(0)} 分 · {c.contribution >= 0 ? '+' : ''}
                      {c.contribution.toFixed(1)}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </Panel>

      {extra}

      {/* 各角度明细 */}
      <div>
        <div className="mb-2 flex items-baseline gap-2">
          <h2 className="text-[15px] font-semibold tracking-wide">多角度明细</h2>
          <span className="text-xs text-ink-faint">
            {scored.length} 个角度参与评分 · 分数以 50 为中性
          </span>
        </div>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {r.angles.map((a) => <AngleCard key={a.id} a={a} />)}
        </div>
      </div>

      {/* 结论 */}
      <Panel title="结论要点">
        <ul className="space-y-1.5 text-sm text-ink-dim">
          {r.verdict.points.map((p, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-ink-faint">·</span><span>{p}</span>
            </li>
          ))}
        </ul>
      </Panel>

      {/* 风险提示 */}
      <Panel title="风险提示" meta="不参与评分，仅作警示">
        {r.risk.flags?.length ? (
          <ul className="space-y-1.5 text-sm">
            {r.risk.flags.map((f, i) => (
              <li key={i} className="flex gap-2 text-down">
                <span>!</span><span>{f}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-ink-faint">
            {r.risk.available ? '未触发风险阈值' : (r.risk.hint ?? '风险指标不可用')}
          </p>
        )}
        {r.risk.metrics?.length ? (
          <div className="mt-3 grid grid-cols-1 gap-x-8 sm:grid-cols-2">
            {r.risk.metrics.map((m) => <MetricRow key={m.key} m={m} />)}
          </div>
        ) : null}
      </Panel>

      {r.verdict.risks.length > 0 && (
        <Panel title="数据缺口与限制">
          <ul className="space-y-1 text-xs text-ink-faint">
            {r.verdict.risks.map((x, i) => (
              <li key={i} className="flex gap-2"><span>·</span><span>{x}</span></li>
            ))}
          </ul>
        </Panel>
      )}

      <p className="text-[11px] leading-relaxed text-ink-faint">{r.disclaimer}</p>
    </div>
  );
}
