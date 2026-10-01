'use client';

/**
 * 因子详情 —— 「研报台」版式：定义 → 快速评价 → 图表 → 历史报告。
 * 数据逻辑与旧版一致（SWR /factors/{name}、评价后 mutate 刷新 reports）；
 * series 为图表数据包（评价成功后拉取，失败不阻塞主结果）。
 */

import { useEffect, useMemo, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import ProgressBar from '@/components/ProgressBar';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
import { del, fetcher, get, post, put } from '@/lib/api';
import { useJobStream } from '@/lib/streaming';
import { C, axes, legend, tooltip } from '@/lib/chart';
import {
  ErrorBanner, GroupIcSection, NeutralLadderPanel, NeutralViewsSection,
  QuantileCharts, RatingPanel, RecipeSteps, RobustnessPanel, StyleCorrPanel, TopNTable,
  decayOption as decayOptionShared, eventStudyOption, excessNavOption, icByYearOption,
  type EvalErrors, type EvalSeries, type PreprocessStep, type RatingInfo, type RobustnessInfo,
} from '../shared';

type FactorDetail = {
  name: string;
  expression: string;
  description: string;
  created_at: string;
  source?: string;
  category?: string;
  reports: { name: string; url: string }[];
};

type EvalResult = {
  factor: string;
  formula: string;
  n_samples: number;
  ic: {
    mean: number; ir: number; t_stat: number; positive_rate: number;
    ic_gt_002_rate: number; t_stat_nw?: number | null; ic_autocorr?: number | null;
  };
  rank_ic_mean: number;
  long_short: { annual_return: number; sharpe: number; max_drawdown: number };
  monotonicity: number;
  half_life: number | null;
  suggested_rebalance: string;
  report_url: string;
  // 本轮新增暴露的字段（详情页同样渲染，避免只算不显示）
  rating?: RatingInfo | null;
  robustness?: RobustnessInfo | null;
  steps?: PreprocessStep[] | null;
  errors?: EvalErrors;
};

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20', 'turnover'];

type BuiltinItem = { name: string; family: string; formula: string };

/** 可编辑来源：与后端 PUT /factors/{name} 同口径 */
const EDITABLE = new Set(['manual', 'mined', undefined]);

export default function FactorDetailPage() {
  const { name = '' } = useParams<{ name: string }>();
  const router = useRouter();
  const { data, error, isLoading, mutate } = useSWR<FactorDetail>(
    name ? `/factors/${name}` : null, fetcher,
  );
  const { data: builtin } = useSWR<BuiltinItem[]>('/factors/builtin', get);
  const [formula, setFormula] = useState('pct_change_20');
  const [res, setRes] = useState<EvalResult | null>(null);
  const [series, setSeries] = useState<EvalSeries | null>(null);
  const [busy, setBusy] = useState(false);
  const [editBusy, setEditBusy] = useState(false);
  const [msg, setMsg] = useState('');
  // 编辑态：定义面板就地展开
  const [editing, setEditing] = useState(false);
  const [expr, setExpr] = useState('');
  const [desc, setDesc] = useState('');
  const [category, setCategory] = useState('');
  // 评价任务流：POST 202 → WS 流式进度 → 终态 result 渲染
  const [evalJob, setEvalJob] = useState<string | null>(null);
  const evalStream = useJobStream<EvalResult & { series?: EvalSeries }>(evalJob);
  // 图表数据包缺失/失败显式提示（与页面主结果分开，避免被当成「样本不足」）
  const [seriesError, setSeriesError] = useState<string | null>(null);

  // 终态回填：result → 指标/图表；error 或 done-无果 → 消息条（busy 防悬挂）
  useEffect(() => {
    if (!evalJob) return;
    if (evalStream.error) {
      setMsg(`✗ ${evalStream.error}`);
      setEvalJob(null);
      setBusy(false);
    } else if (evalStream.result) {
      setRes(evalStream.result);
      // 图表数据包随主评价一次返回（失败不阻塞主结果）
      setSeries(evalStream.result.series ?? null);
      setSeriesError(evalStream.result.series
        ? null
        : '图表数据缺失：评价响应未包含 series 字段');
      mutate(); // 评价后 reports 列表可能新增
      setEvalJob(null);
      setBusy(false);
    } else if (evalStream.done) {
      // done 但无 result/error（not_found / canceled 等）：终态兜底，防 busy 悬挂
      setMsg(`✗ 评价任务异常结束（${evalStream.status ?? 'unknown'}）`);
      setEvalJob(null);
      setBusy(false);
    }
  }, [evalJob, evalStream, mutate]);

  // —— 图表 option（依赖 series） ——
  const cumOption = useMemo(() => {
    if (!series?.ic.dates.length) return null;
    return {
      tooltip,
      legend: legend({ top: 0 }),
      grid: { left: 48, right: 52, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      ...axes({ data: series.ic.dates }),
      yAxis: [
        { type: 'value' as const, name: 'IC', nameTextStyle: { color: C.inkDim }, axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10 } },
        { type: 'value' as const, name: '累计IC', nameTextStyle: { color: C.inkDim }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      ],
      series: [
        {
          name: '日度IC', type: 'bar' as const, data: series.ic.ic,
          itemStyle: {
            color: (p: { data: number | null }) => (p.data == null || p.data >= 0 ? C.up : C.down),
          },
        },
        { name: 'RankIC', type: 'line' as const, data: series.ic.rank_ic, showSymbol: false, lineStyle: { width: 1, color: C.down }, itemStyle: { color: C.down } },
        { name: '累计IC', type: 'line' as const, yAxisIndex: 1, data: series.ic.cum_ic, showSymbol: false, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
      ],
    };
  }, [series]);

  const rollingOption = useMemo(() => {
    if (!series?.rolling?.dates.length) return null;
    const r = series.rolling;
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(4) },
      legend: legend({ top: 0 }),
      grid: { left: 48, right: 20, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      ...axes({ data: r.dates }, { name: '滚动均值' }),
      series: [
        { name: `RankIC(${r.window}日)`, type: 'line' as const, data: r.rank_ic, showSymbol: false, lineStyle: { width: 1.8, color: C.indigo }, itemStyle: { color: C.indigo } },
        { name: `IC(${r.window}日)`, type: 'line' as const, data: r.ic, showSymbol: false, lineStyle: { width: 1.5, color: C.up }, itemStyle: { color: C.up } },
      ],
    };
  }, [series]);

  const rollingIrOption = useMemo(() => {
    if (!series?.rolling?.dates.length) return null;
    const r = series.rolling;
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
      grid: { left: 48, right: 20, top: 20, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      ...axes({ data: r.dates }, { name: 'IR' }),
      series: [{
        name: `IR(${r.window}日)`, type: 'line' as const, data: r.ir,
        showSymbol: false, lineStyle: { width: 1.5, color: C.inkDim }, itemStyle: { color: C.inkDim },
      }],
    };
  }, [series]);

  // 其余序列图统一走共享 option 工厂，与快速评价页维持同一口径
  const decayChartOption = useMemo(
    () => (series ? decayOptionShared(series) : null), [series]);
  const icYearOption = useMemo(
    () => (series ? icByYearOption(series) : null), [series]);
  const excessOption = useMemo(
    () => (series ? excessNavOption(series) : null), [series]);
  const eventOption = useMemo(
    () => (series ? eventStudyOption(series) : null), [series]);

  async function remove() {
    if (!window.confirm(`确认删除因子 ${name}？历史报告与挖掘台账会保留。`)) return;
    setEditBusy(true);
    setMsg('');
    try {
      await del(`/factors/${name}`);
      router.push('/factors?tab=library');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
      setEditBusy(false);
    }
  }

  async function saveEdit() {
    setEditBusy(true);
    setMsg('');
    try {
      await put(`/factors/${name}`, {
        expression: expr,
        description: desc,
        category: category.trim() || null,
      });
      setEditing(false);
      await mutate();
      setMsg('✓ 已保存');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setEditBusy(false);
    }
  }

  async function evaluate() {
    setBusy(true);
    setMsg('');
    try {
      const params = { factor: name, formula };
      // 评价任务化：202 {job_id} → WS 流式进度 → 终态 result 渲染
      const r = await post<{ job_id: string; status: string }>('/factors/evaluate', params);
      setEvalJob(r.job_id);
      setMsg('评价任务已排队');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
      setBusy(false);
    }
    // busy 在流式结果/错误回流时清除（见下方 effect）
  }

  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorNote>因子不存在或加载失败</ErrorNote>;

  return (
    <div className="space-y-5">
      <PageHeader
        title={data.name}
        sub={`注册于 ${data.created_at?.slice(0, 19)}`}
      />

      {/* 定义 */}
      <Panel
        title="定义"
        actions={
          EDITABLE.has(data.source) ? (
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  setExpr(data.expression ?? '');
                  setDesc(data.description ?? '');
                  setCategory(data.category === '自定义' ? '' : (data.category ?? ''));
                  setEditing(true);
                }}
                className="text-sm text-indigo hover:underline"
                disabled={editBusy}
              >
                编辑
              </button>
              <button
                onClick={remove}
                className="text-sm text-down hover:underline"
                disabled={editBusy}
              >
                删除
              </button>
            </div>
          ) : (
            <span className="text-xs text-ink-faint">种子灌入因子不可编辑</span>
          )
        }
      >
        {!editing ? (
          <>
            {data.expression ? (
              <code className="block bg-paper px-3 py-2 font-mono text-sm">{data.expression}</code>
            ) : (
              <div className="text-sm text-ink-faint">未登记 DSL 表达式（快速评价可用现算公式）</div>
            )}
            {data.description && <p className="mt-2 text-sm text-ink-dim">{data.description}</p>}
          </>
        ) : (
          <div className="space-y-3">
            <label className="block">
              <span className="mb-1 block text-xs text-ink-dim">DSL 表达式（AST 校验，留空 = 仅评分用现算公式）</span>
              <input
                value={expr}
                onChange={(e) => setExpr(e.target.value)}
                className="input input-mono w-full font-mono text-xs"
                placeholder="Rank(Ts_Mean($close,5)/$close-1)"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-ink-dim">描述</span>
              <textarea value={desc} onChange={(e) => setDesc(e.target.value)}
                rows={2} className="input w-full text-xs" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-ink-dim">类别（留空归入「自定义」）</span>
              <input value={category} onChange={(e) => setCategory(e.target.value)}
                className="input w-56 text-xs" placeholder="动量 / 波动率 / 量价 …" />
            </label>
            <div className="flex gap-2">
              <button onClick={saveEdit} className="btn btn-sm btn-primary" disabled={editBusy}>
                {editBusy ? '保存中…' : '保存'}
              </button>
              <button onClick={() => setEditing(false)} className="btn btn-sm" disabled={editBusy}>
                取消
              </button>
            </div>
          </div>
        )}
      </Panel>

      {/* 快速评价 */}
      <Panel title="快速评价" meta="基于数据湖日线现算，IC / 分层 / 衰减一次跑完">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <input
            value={formula}
            onChange={(e) => setFormula(e.target.value)}
            list="builtin-factors-detail"
            placeholder="MA20 / RSV10 / CORR60 / pct_change_20 …"
            className="input input-mono w-64"
          />
          <datalist id="builtin-factors-detail">
            {(builtin ?? []).map((b) => <option key={b.name} value={b.name}>{b.formula}</option>)}
            {FORMULAS.map((f) => <option key={f} value={f} />)}
          </datalist>
          <div className="flex flex-wrap gap-1">
            {FORMULAS.map((f) => (
              <button key={f} onClick={() => setFormula(f)}
                className={`tag ${formula === f ? 'tag-on' : ''}`}>
                {f}
              </button>
            ))}
          </div>
          <button
            onClick={evaluate}
            disabled={busy}
            className="btn btn-accent"
          >
            {busy ? '评价中…' : '运行评价'}
          </button>
          {evalJob && evalStream.progress && evalStream.progress.total > 0 && (
            <div className="w-64">
              <ProgressBar
                pct={(evalStream.progress.done / evalStream.progress.total) * 100}
                phase={evalStream.progress.phase}
              />
              <div className="text-[11px] text-ink-faint">任务 {evalJob.slice(0, 8)}…</div>
            </div>
          )}
        </div>
        <Msg text={msg} />
        {res && (
          <div className={msg ? 'mt-4' : ''}>
            <div className="grid grid-cols-2 gap-y-4 divide-line border-t border-line pt-4 md:grid-cols-4 lg:grid-cols-6 md:divide-x">
              <div className="pr-4">
                <Stat label={`IC 均值（${res.formula}）`} value={res.ic.mean}
                  tone={res.ic.mean > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="ICIR" value={res.ic.ir} />
              </div>
              <div className="px-4">
                <Stat label="t 统计量" value={res.ic.t_stat} />
              </div>
              <div className="px-4">
                <Stat label="RankIC 均值" value={res.rank_ic_mean} />
              </div>
              <div className="px-4">
                <Stat label="阈值胜率" value={res.ic.ic_gt_002_rate != null ? `${(res.ic.ic_gt_002_rate * 100).toPrecision(3)}%` : '—'} />
              </div>
              <div className="px-4">
                <Stat label="多空年化" value={`${(res.long_short.annual_return * 100).toFixed(2)}%`}
                  tone={res.long_short.annual_return > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="多空夏普" value={res.long_short.sharpe} />
              </div>
              <div className="px-4 pt-4 md:pt-0 md:border-0 border-t border-line">
                <Stat label="多空最大回撤" value={`${(res.long_short.max_drawdown * 100).toFixed(2)}%`} tone="text-down" />
              </div>
              <div className="px-4">
                <Stat label="单调性" value={res.monotonicity} />
              </div>
              <div className="px-4">
                <Stat label="半衰期" value={res.half_life != null ? `${res.half_life} 天` : '—'} />
              </div>
              <div className="px-4">
                <Stat label="建议调仓" value={<span className="text-base">{res.suggested_rebalance}</span>} />
              </div>
              <div className="px-4">
                <Stat label="样本数" value={res.n_samples.toLocaleString()} />
              </div>
              <div className="px-4">
                <Stat label="完整报告" value={
                  <a href={res.report_url} target="_blank" className="font-sans text-sm text-indigo hover:underline" rel="noreferrer">
                    查看 ↗
                  </a>
                } />
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* 评级 / 稳健性 / 配方：详情页同样展示，避免「后端算了、前端看不到」 */}
      {res && (
        <div className="grid gap-5 lg:grid-cols-2">
          <RatingPanel rating={res.rating} />
          <RobustnessPanel robustness={res.robustness} />
        </div>
      )}

      {res && (
        <Panel title="预处理配方" meta="本次评价实际生效的 steps">
          <RecipeSteps steps={res.steps} />
        </Panel>
      )}

      {/* 计算失败显式可见（metrics.errors 与 series.errors 合并去重） */}
      <ErrorBanner
        errors={{ ...res?.errors, ...series?.errors }}
        title="评价过程有计算失败"
      />

      {/* 图表数据包缺失/失败：与主结果分开提示 */}
      {seriesError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {seriesError}
        </div>
      )}

      {/* 图表区：IC 序列 / 滚动窗口 / 分层 / 衰减 / 归因 / 中性化 / 分组 IC */}
      {series && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="IC 序列与累计 IC">
            {cumOption
              ? <Chart option={cumOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="滚动窗口指标" meta={`窗口 ${series.rolling?.window ?? '—'} 交易日 · 掉头向下/转负 = 阶段性失效预警`}>
            {rollingOption
              ? <Chart option={rollingOption} height={260} />
              : <Empty>样本不足（需要 ≥ 60 个交易日）</Empty>}
          </Panel>
          <Panel title="滚动窗口 IR" meta="IR 掉头向下 = 稳定性恶化">
            {rollingIrOption
              ? <Chart option={rollingIrOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <QuantileCharts series={series} />
          <Panel title="IC 衰减" meta={`半衰期 ${res?.half_life ?? '—'} 天 → 建议 ${res?.suggested_rebalance ?? '—'}`}>
            {decayChartOption
              ? <Chart option={decayChartOption} height={240} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="分年度 IC" meta="突降 = 因子反转预警">
            {icYearOption
              ? <Chart option={icYearOption} height={240} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="超额净值曲线" meta={`相对${series.excess?.benchmark ?? '股票池等权'}基准 · 稳定上行 = 真超额`}>
            {excessOption
              ? <Chart option={excessOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel
            title="事件式分层收益"
            meta={series.event_study?.rel_periods.length
              ? `事件日 ±${series.event_study.after} 日 · 事前/事后发散比 ${
                series.event_study.look_ahead_ratio?.toFixed(2) ?? '—'}（<1 才是预测信号）`
              : '事件日前后各 N 日'}
          >
            {eventOption
              ? <Chart option={eventOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <div className="space-y-5">
            <NeutralLadderPanel
              rows={series.neutral_ladder}
              returnNeutralIc={series.neutral_views?.return_neutral_ic}
            />
            <NeutralViewsSection views={series.neutral_views} />
            <GroupIcSection groupIc={series.group_ic} />
            <StyleCorrPanel styleCorr={series.style_corr} />
          </div>
        </div>
      )}

      {series && (series.top_n?.length ?? 0) > 0 && (
        <Panel title="Top-N 持仓收缩">
          <TopNTable rows={series.top_n} />
        </Panel>
      )}

      {/* 历史报告 */}
      <Panel title="历史报告" meta={`${data.reports.length} 份`}>
        {!data.reports.length ? (
          <Empty>暂无 —— 跑一次评价即生成</Empty>
        ) : (
          <ul className="divide-y divide-line text-sm">
            {data.reports.map((r) => (
              <li key={r.name} className="py-2">
                <a href={r.url} target="_blank" className="text-indigo hover:underline" rel="noreferrer">
                  {r.name} ↗
                </a>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
