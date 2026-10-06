'use client';

/**
 * 因子研究 —— 「研报台」版式：
 * 注册(DSL) + 快速评价 → 评价结果指标条 → IC/分层/衰减图表 → 相关性/合成 → 已注册因子。
 * 数据逻辑与旧版一致；evalSeries 为图表数据包（评价成功后拉取，失败不阻塞主结果）。
 */

import { useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import ProgressBar from '@/components/ProgressBar';
import { Empty, Msg } from '@/components/States';
import { get, post } from '@/lib/api';
import { useJobStream } from '@/lib/streaming';
import { C, axes, legend, tooltip } from '@/lib/chart';
import FactorLibrary from './FactorLibrary';
import QlibWorkflowPanel from './QlibWorkflowPanel';
import SaveAsFactor from './SaveAsFactor';
import {
  ErrorBanner, NeutralLadderPanel, NeutralViewsSection,
  RatingPanel, RecipeSteps, RobustnessPanel, StyleCorrPanel, GroupIcSection, TopNTable,
  decayOption as decayOptionShared, icByYearOption,
  eventStudyOption, excessNavOption, groupReturnOption, quantileNavOption,
  type EvalErrors, type EvalSeries, type PreprocessStep, type RatingInfo,
  type RobustnessInfo, type TopNRow,
} from './shared';

type FactorRow = {
  name: string; expression: string; description: string; created_at: string;
  source?: string; ic_neutral?: number | null; category?: string;
};
type EvalResult = {
  factor: string;
  formula: string;
  n_samples: number;
  ic: {
    mean: number | null; ir: number | null; t_stat: number | null;
    positive_rate: number | null; ic_gt_002_rate?: number | null;
    t_stat_nw?: number | null; ic_autocorr?: number | null;
  };
  rank_ic_mean: number | null;
  long_short: { annual_return: number | null; sharpe: number | null; max_drawdown: number | null };
  monotonicity: number | null;
  half_life: number | null;
  suggested_rebalance: string;
  excess: { annual_excess?: number | null; excess_sharpe?: number | null; excess_mdd?: number | null };
  annual_turnover: number | null;
  top_n: TopNRow[];
  style_corr: { max_abs: number | null; passed: boolean | null };
  outlier?: { threshold: number; n_dropped: number; dropped_rate: number } | null;
  report_url: string;
  // —— 本轮新增暴露的字段 ——
  rating?: RatingInfo | null;
  robustness?: RobustnessInfo | null;
  /** 实际生效的预处理配方；null = 未显式传 steps（内置默认口径） */
  steps?: PreprocessStep[] | null;
  covariates?: Record<string, number>;
  errors?: EvalErrors;
};

/** 预处理方法枚举（GET /factors/preprocess/methods） */
type PreprocessMethod = {
  name: string; stage: string; label: string; params?: Record<string, unknown>;
  /** 口径自省：数学定义 / 补充说明 / 零方差语义（见后端 registry 元数据词汇表） */
  formula?: string; notes?: string; zero_variance?: string;
};
type MadConvention = {
  scale_factor: number; formula: string; n_semantics: string;
  alphapurify_conversion: string; note: string;
};
type PreprocessMethods = {
  stages: string[];
  methods: PreprocessMethod[];
  default_recipe: PreprocessStep[];
  mad_convention?: MadConvention;
};
type CorrResult = {
  factors: string[];
  matrix: number[][];
  redundant_pairs: { a: string; b: string; corr: number; verdict: string }[];
  n_dates: number;
};
type SynResult = {
  factor: string;
  method: string;
  n_samples: number;
  ic: { mean: number; ir: number; t_stat: number };
  long_short: { annual_return: number; sharpe: number; max_drawdown: number };
  report_url: string;
};

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20', 'turnover',
  'MA20', 'RSV10', 'ROC5', 'KMID', 'WVMA20', 'CNTP10', 'CORR20'];
type BuiltinItem = { name: string; family: string; window: number | null; formula: string };

/** 预处理阶段 → 中文标签（方法名一律来自后端注册表，不在这里硬编码） */
const STAGE_LABEL: Record<string, string> = {
  winsorize: '去极值', standardize: '标准化', neutralize: '中性化', orthogonalize: '正交化',
};

/** 注册表逻辑协变量名 → 后端评价数据帧里的实际列名（cov_ 前缀） */
const COV_ALIASES = new Set(['market_cap', 'industry_sw1', 'turnover_1m', 'momentum_1m']);

function corrColor(v: number): string {
  const a = Math.min(Math.abs(v), 1);
  const blue = `rgba(49,88,158,${(a * 0.75).toFixed(2)})`;   // 靛 · 正相关
  const red = `rgba(195,53,43,${(a * 0.75).toFixed(2)})`;    // 朱 · 负相关
  return v >= 0 ? blue : red;
}

export default function FactorsPage() {
  const { data: factors, mutate } = useSWR<FactorRow[]>('/factors', get);
  const { data: builtin } = useSWR<BuiltinItem[]>('/factors/builtin', get);
  const { data: universes } = useSWR<{ key: string; index_code: string | null; label: string }[]>(
    '/factors/universes', get);
  const [tab, setTab] = useState<'eval' | 'lab' | 'library' | 'qlib'>('eval');
  const [formula, setFormula] = useState('pct_change_20');
  const [evalRes, setEvalRes] = useState<EvalResult | null>(null);
  const [evalSeries, setEvalSeries] = useState<EvalSeries | null>(null);
  // 评价任务流：POST 返回 job_id → WS 流式进度 → 终态 result 渲染
  const [evalJob, setEvalJob] = useState<string | null>(null);
  const evalStream = useJobStream<EvalResult & { series?: EvalSeries }>(evalJob);
  const [seriesError, setSeriesError] = useState<string | null>(null);
  const [busy, setBusy] = useState<'' | 'eval' | 'corr' | 'syn'>('');
  const [msg, setMsg] = useState('');
  // 评价范围：时间区间 + 股票池
  const [evalStart, setEvalStart] = useState('2026-01-01');
  const [evalEnd, setEvalEnd] = useState('');
  const [evalUniverse, setEvalUniverse] = useState('all');
  // 相关性 / 合成
  const [picked, setPicked] = useState<string[]>(['pct_change_20', 'rolling_std_20']);
  const [corr, setCorr] = useState<CorrResult | null>(null);
  const [syn, setSyn] = useState<SynResult | null>(null);
  const [customFormula, setCustomFormula] = useState('');
  const [corrStart, setCorrStart] = useState('2026-01-01');
  const [corrEnd, setCorrEnd] = useState('');
  const [corrUniverse, setCorrUniverse] = useState('all');
  const [corrThreshold, setCorrThreshold] = useState(0.8);
  const [zThreshold, setZThreshold] = useState('');
  // 预处理配方：默认配方 = 不传 steps（后端内置口径）；自定义 = 按阶段各选一个方法
  const { data: preprocess } = useSWR<PreprocessMethods>('/factors/preprocess/methods', get);
  const [recipeMode, setRecipeMode] = useState<'default' | 'custom'>('default');
  const [recipeChoice, setRecipeChoice] = useState<Record<string, string>>({});
  // 稳健性检验为可选：默认关闭（要按扰动窗口重算因子多遍，明显更慢）
  const [withRobustness, setWithRobustness] = useState(false);

  /** 自定义配方 → steps 数组（按 stages 顺序；未选方法的阶段跳过） */
  const customSteps: PreprocessStep[] = useMemo(() => {
    const stages = preprocess?.stages ?? [];
    const methods = preprocess?.methods ?? [];
    const out: PreprocessStep[] = [];
    for (const stage of stages) {
      const name = recipeChoice[stage];
      if (!name) continue;
      const m = methods.find((x) => x.stage === stage && x.name === name);
      if (!m) continue;
      const step: PreprocessStep = { op: stage, method: name, ...(m.params ?? {}) };
      // 注册表里的协变量用逻辑名（market_cap/industry_sw1），后端评价时已统一
      // 落成 cov_* 列（build_covariates）；不改名会以「列不存在」422 收场
      if (Array.isArray(step.factors)) {
        step.factors = (step.factors as string[]).map((f) => (COV_ALIASES.has(f) ? `cov_${f}` : f));
      }
      out.push(step);
    }
    return out;
  }, [preprocess, recipeChoice]);

  /** 本次评价要发送的 steps：默认配方用 undefined（JSON 里省略） */
  const stepsPayload = recipeMode === 'custom' ? customSteps : undefined;

  // —— 评价图表（依赖 evalSeries） ——
  const icOption = useMemo(() => {
    if (!evalSeries?.ic.dates.length) return null;
    return {
      tooltip,
      legend: legend({ top: 0 }),
      grid: { left: 48, right: 52, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      ...axes({ data: evalSeries.ic.dates }),
      yAxis: [
        { type: 'value' as const, name: 'IC', nameTextStyle: { color: C.inkDim }, axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10 } },
        { type: 'value' as const, name: '累计IC', nameTextStyle: { color: C.inkDim }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10 } },
      ],
      series: [
        {
          name: '日度IC', type: 'bar' as const, data: evalSeries.ic.ic,
          itemStyle: {
            color: (p: { data: number | null }) =>
              (p.data == null ? C.inkDim : p.data >= 0 ? C.up : C.down),
          },
        },
        { name: '累计IC', type: 'line' as const, yAxisIndex: 1, data: evalSeries.ic.cum_ic, showSymbol: false, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
      ],
    };
  }, [evalSeries]);

  const quantileOption = useMemo(
    () => (evalSeries ? quantileNavOption(evalSeries) : null), [evalSeries]);

  const groupOption = useMemo(
    () => (evalSeries ? groupReturnOption(evalSeries) : null), [evalSeries]);

  const decayOption = useMemo(
    () => (evalSeries ? decayOptionShared(evalSeries) : null), [evalSeries]);

  const icYearOption = useMemo(
    () => (evalSeries ? icByYearOption(evalSeries) : null), [evalSeries]);

  const rollingOption = useMemo(() => {
    if (!evalSeries?.rolling?.dates.length) return null;
    const r = evalSeries.rolling;
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
  }, [evalSeries]);

  const excessOption = useMemo(
    () => (evalSeries ? excessNavOption(evalSeries) : null), [evalSeries]);

  const eventOption = useMemo(
    () => (evalSeries ? eventStudyOption(evalSeries) : null), [evalSeries]);

  // 评价任务流回填：终态 result → 指标/图表渲染；error / done-无果 → 消息条
  useEffect(() => {
    if (!evalJob) return;
    if (evalStream.error) {
      setMsg(`✗ ${evalStream.error}`);
      setEvalJob(null);
      setBusy('');
    } else if (evalStream.result) {
      setEvalRes(evalStream.result);
      setEvalSeries(evalStream.result.series ?? null);
      setSeriesError(evalStream.result.series ? null : '图表数据缺失：评价响应未包含 series 字段');
      setEvalJob(null);
      setBusy('');
    } else if (evalStream.done) {
      setMsg(`✗ 评价任务异常结束（${evalStream.status ?? 'unknown'}）`);
      setEvalJob(null);
      setBusy('');
    }
  }, [evalJob, evalStream.error, evalStream.result, evalStream.done, evalStream.status]);

  function toggle(f: string) {
    setPicked((p) => (p.includes(f) ? p.filter((x) => x !== f) : [...p, f]));
  }

  function addCustom() {
    const f = customFormula.trim();
    if (!f || picked.includes(f) || picked.length >= 8) return;
    setPicked((p) => [...p, f]);
    setCustomFormula('');
  }

  async function evaluate() {
    setBusy('eval');
    setMsg('');
    try {
      const params = {
        // 报告名仅允许字母数字下划线（后端防路径穿越校验），从公式派生并清洗
        factor: (formula.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64) || 'tmp'),
        // 必须显式传公式：后端 EvaluateIn.formula 默认 pct_change_20，
        // 漏发的话选什么因子实际都在评 pct_change_20
        formula,
        // 分层组数：与 CLI/报告默认值一致（10 分位）。此前这里写死 5，
        // 于是「页面上看到 5 组、报告里也是 5 组、CLI 却是 10 组」——
        // 同一平台两种口径。默认值收口在后端 defaults.py。
        n_groups: 10,
        start: evalStart,
        end: evalEnd.trim() ? evalEnd : null,
        universe: evalUniverse,
        // NaN 会被 JSON.stringify 序列化成 null → 后端静默按「不过滤」处理；
        // 非法输入必须提示而不是静默改变评价口径
        filter_zscore: (() => {
          const t = zThreshold.trim();
          if (!t) return null;
          const n = Number(t);
          if (!Number.isFinite(n) || n < 1) {
            throw new Error('截面过滤阈值需为 ≥1 的数字');
          }
          return n;
        })(),
        event_window: [10, 15],
        // 预处理配方：默认配方省略（undefined → JSON 里没有该键）
        ...(stepsPayload && stepsPayload.length ? { steps: stepsPayload } : {}),
        with_robustness: withRobustness,
      };
      // 评价任务化：202 {job_id}，进度条与结果经 /ws/jobs/{id} 流式回流
      const r = await post<{ job_id: string; status: string }>('/factors/evaluate', params);
      setEvalJob(r.job_id);
      setMsg('评价任务已排队，进度实时更新');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
      setBusy('');
    }
    // busy 在流式结果/错误回流时清除（见下方 effect）
  }

  async function analyze() {
    setBusy('corr');
    setMsg('');
    try {
      setCorr(await post<CorrResult>('/factors/analyze', {
        formulas: picked, start: corrStart,
        end: corrEnd.trim() ? corrEnd : null,
        universe: corrUniverse,
        threshold: corrThreshold,
      }));
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function doSynthesize(method: 'equal' | 'ic_weighted') {
    setBusy('syn');
    setMsg('');
    try {
      setSyn(await post<SynResult>('/factors/synthesize', {
        formulas: picked, method, start: corrStart,
        end: corrEnd.trim() ? corrEnd : null,
        universe: corrUniverse,
      }));
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="因子"
        sub={<>快速评价（IC / 分层 / 衰减）· 相关性与合成 · 因子库（分类管理）{builtin ? ` · 内置 Qlib Alpha158 ${builtin.length} 个` : ''}</>}
      />

      {/* 顶部 Tab：评价 / 相关性·合成 / 因子库 */}
      <div className="flex flex-wrap items-center gap-1">
        {([
          ['eval', '快速评价'],
          ['lab', '相关性 · 合成'],
          ['library', '因子库'],
          ['qlib', 'Qlib 工作流'],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`tag ${tab === key ? 'tag-on' : ''}`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'eval' && (
      <Panel title="快速评价" meta="IC / 分层 / 衰减 · 一次跑完">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={formula}
              onChange={(e) => setFormula(e.target.value)}
              list="builtin-factors"
              placeholder="MA20 / RSV10 / ROC5 / KMID / pct_change_20 …"
              className="input input-mono w-72"
            />
            <datalist id="builtin-factors">
              {(builtin ?? []).map((b) => <option key={b.name} value={b.name}>{b.formula}</option>)}
            </datalist>
          </div>
          <div className="flex flex-wrap gap-1">
            {FORMULAS.slice(5).map((f) => (
              <button
                key={f}
                onClick={() => setFormula(f)}
                className={`tag ${formula === f ? 'tag-on' : ''}`}
              >
                {f}
              </button>
            ))}
          </div>
          {/* 时间范围 + 股票池 */}
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-ink-dim">
              开始
              <input type="date" value={evalStart} onChange={(e) => setEvalStart(e.target.value)}
                className="input w-36 py-1 text-xs" />
            </label>
            <label className="flex items-center gap-1 text-xs text-ink-dim">
              结束
              <input type="date" value={evalEnd} onChange={(e) => setEvalEnd(e.target.value)}
                className="input w-36 py-1 text-xs" />
            </label>
            <label className="flex items-center gap-1 text-xs text-ink-dim">
              股票池
              <select
                value={evalUniverse}
                onChange={(e) => setEvalUniverse(e.target.value)}
                className="input w-32 py-1 text-xs"
              >
                {(universes ?? [{ key: 'all', index_code: null, label: '全市场' }]).map((u) => (
                  <option key={u.key} value={u.key}>{u.label}</option>
                ))}
              </select>
            </label>
            {evalUniverse !== 'all' && (
              <span className="text-xs text-ink-faint">
                成分快照为最新一期（研究口径）；成分表为空时请先在数据页同步
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <input
              value={zThreshold}
              onChange={(e) => setZThreshold(e.target.value)}
              placeholder="留空 = 不过滤"
              inputMode="decimal"
              className="input input-mono w-32"
              title="截面异常收益过滤阈值（|z| 上限，口径同 alphalens）"
            />
            <span className="text-xs text-ink-faint">
              截面异常收益过滤 |z| 上限（留空不过滤；20 为研报默认口径）
            </span>
          </div>
          {/* 预处理配方：方法名单一来自后端注册表，避免前端硬编码漂移 */}
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-ink-dim">
              预处理配方
              <select
                value={recipeMode}
                onChange={(e) => setRecipeMode(e.target.value === 'custom' ? 'custom' : 'default')}
                className="input w-32 py-1 text-xs"
              >
                <option value="default">内置默认</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            {recipeMode === 'custom' && (preprocess?.stages ?? []).map((stage) => (
              <label key={stage} className="flex items-center gap-1 text-xs text-ink-dim">
                {STAGE_LABEL[stage] ?? stage}
                <select
                  value={recipeChoice[stage] ?? ''}
                  onChange={(e) => setRecipeChoice((p) => ({ ...p, [stage]: e.target.value }))}
                  className="input w-36 py-1 text-xs"
                >
                  <option value="">不启用</option>
                  {(preprocess?.methods ?? [])
                    .filter((m) => m.stage === stage)
                    .map((m) => (
                      <option
                        key={m.name}
                        value={m.name}
                        /* 口径自省：悬停即见数学定义与零方差语义，避免「同名不同义」误用 */
                        title={[m.formula, m.notes, m.zero_variance && `零方差：${m.zero_variance}`]
                          .filter(Boolean).join('\n')}
                      >
                        {m.label}
                      </option>
                    ))}
                </select>
                {(() => {
                  const sel = (preprocess?.methods ?? [])
                    .find((m) => m.stage === stage && m.name === recipeChoice[stage]);
                  if (!sel?.formula) return null;
                  return (
                    <span className="text-ink-faint" title={sel.notes ?? ''}>
                      {sel.formula}
                    </span>
                  );
                })()}
              </label>
            ))}
            {recipeMode === 'custom' && preprocess?.mad_convention && (
              <span
                className="text-ink-faint"
                title={`${preprocess.mad_convention.note}\n换算：${preprocess.mad_convention.alphapurify_conversion}`}
              >
                MAD 口径：{preprocess.mad_convention.formula}
              </span>
            )}
            {recipeMode === 'custom' && !preprocess && (
              <span className="text-xs text-ink-faint">预处理方法清单加载失败，暂只能用内置默认配方</span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={evaluate}
              disabled={busy === 'eval' || !formula}
              className="btn btn-accent"
            >
              {busy === 'eval' ? '评价中…' : '运行评价'}
            </button>
            <label className="flex items-center gap-1 text-xs text-ink-dim">
              <input
                type="checkbox"
                checked={withRobustness}
                onChange={(e) => setWithRobustness(e.target.checked)}
              />
              稳健性检验（窗口扰动 / 分段稳定 / 起点敏感 / 月度剔除，较慢）
            </label>
          </div>
          {evalJob && evalStream.progress && evalStream.progress.total > 0 && (
            <div className="w-64">
              <ProgressBar
                pct={(evalStream.progress.done / evalStream.progress.total) * 100}
                phase={evalStream.progress.phase}
              />
            </div>
          )}
          <p className="text-xs text-ink-faint">
            输入任意内置因子名（如 BETA20、CORR60）或传统公式（pct_change_n / rolling_std_n / turnover）。
          </p>
        </div>
      </Panel>
      )}

      <Msg text={msg} />

      {tab === 'eval' && evalRes && (
        <Panel
          title="评价结果"
          meta={<>{evalRes.factor} · 样本 {evalRes.n_samples}</>}
          actions={
            <div className="flex items-center gap-3">
              <SaveAsFactor formula={formula} onSaved={mutate} />
              <a
                href={evalRes.report_url}
                target="_blank"
                className="text-sm text-indigo hover:underline"
                rel="noreferrer"
              >
                查看完整报告 ↗
              </a>
            </div>
          }
        >
          <div className="grid grid-cols-2 gap-y-4 divide-line md:grid-cols-4 lg:grid-cols-8 md:divide-x">
            <div className="pr-4">
              <Stat label="IC 均值" value={evalRes.ic.mean ?? '—'}
                tone={(evalRes.ic.mean ?? 0) > 0 ? 'text-up' : 'text-down'} />
            </div>
            <div className="px-4">
              <Stat label="ICIR" value={evalRes.ic.ir ?? '—'} />
            </div>
            <div className="px-4">
              <Stat label="t 统计量" value={evalRes.ic.t_stat ?? '—'} />
            </div>
            <div className="px-4">
              {/* 后端把非有限值统一转 null（_jf）：null 参与算术会变成 0.00%，
                  必须显式判空，否则「算不出来」显示成「收益为 0」 */}
              <Stat label="多空年化"
                value={evalRes.long_short.annual_return != null
                  ? `${(evalRes.long_short.annual_return * 100).toFixed(2)}%` : '—'}
                tone={(evalRes.long_short.annual_return ?? 0) > 0 ? 'text-up' : 'text-down'} />
            </div>
            <div className="px-4">
              <Stat label="多空夏普" value={evalRes.long_short.sharpe ?? '—'} />
            </div>
            <div className="px-4">
              <Stat label="单调性" value={evalRes.monotonicity ?? '—'} />
            </div>
            <div className="px-4">
              <Stat label="半衰期" value={evalRes.half_life ?? '—'} hint={evalRes.half_life != null ? '天' : undefined} />
            </div>
            <div className="px-4">
              <Stat label="建议调仓" value={<span className="text-base">{evalRes.suggested_rebalance}</span>} />
            </div>
          </div>
        </Panel>
      )}

      {/* 评级 / 稳健性：L2 结论与 L3 可选项（二者并列，避免长条堆叠） */}
      {tab === 'eval' && evalRes && (
        <div className="grid gap-5 lg:grid-cols-2">
          <RatingPanel rating={evalRes.rating} />
          <RobustnessPanel robustness={evalRes.robustness} />
        </div>
      )}

      {tab === 'eval' && evalRes && (
        <Panel title="预处理配方" meta="本次评价实际生效的 steps（看数字是用哪套配方跑出来的）">
          <RecipeSteps steps={evalRes.steps} />
        </Panel>
      )}

      {tab === 'eval' && (
        <ErrorBanner
          errors={{ ...evalRes?.errors, ...evalSeries?.errors }}
          title="评价过程有计算失败"
        />
      )}

      {tab === 'eval' && evalRes && (
        <Panel
          title="超额与持仓收缩"
          meta={`基准：股票池等权 · 几何超额口径${evalRes.style_corr?.passed != null ? (evalRes.style_corr.passed ? ' · 风格相关性 ✓ 达标' : ' · 风格相关性 ⚠ 超阈值') : ''}`}
        >
          <div className="grid grid-cols-2 gap-y-4 divide-line md:grid-cols-5 md:divide-x">
            <div className="pr-4">
              <Stat label="年化超额(最高组)"
                value={evalRes.excess?.annual_excess != null ? `${(evalRes.excess.annual_excess * 100).toFixed(2)}%` : '—'}
                tone={(evalRes.excess?.annual_excess ?? 0) > 0 ? 'text-up' : 'text-down'} />
            </div>
            <div className="px-4">
              <Stat label="超额夏普" value={evalRes.excess?.excess_sharpe ?? '—'} />
            </div>
            <div className="px-4">
              <Stat label="超额最大回撤"
                value={evalRes.excess?.excess_mdd != null ? `${(evalRes.excess.excess_mdd * 100).toFixed(2)}%` : '—'}
                tone="text-down" />
            </div>
            <div className="px-4">
              <Stat label="年化换手(多空)"
                value={evalRes.annual_turnover != null ? `${(evalRes.annual_turnover * 100).toFixed(0)}%` : '—'} />
            </div>
            <div className="px-4">
              <Stat label="风格相关 max|ρ|"
                value={evalRes.style_corr?.max_abs != null ? evalRes.style_corr.max_abs.toFixed(3) : '—'}
                hint="阈值 0.14" />
            </div>
            <div className="px-4">
              <Stat label="异常收益剔除"
                value={evalRes.outlier ? `${evalRes.outlier.n_dropped}` : '—'}
                hint={evalRes.outlier
                  ? `|z|>${evalRes.outlier.threshold} · 占 ${(evalRes.outlier.dropped_rate * 100).toFixed(3)}%`
                  : '未开启过滤'} />
            </div>
          </div>
          <TopNTable rows={evalRes.top_n} />
        </Panel>
      )}

      {/* 图表加载失败显式提示（缺陷 #4：不再伪装成「样本不足」） */}
      {tab === 'eval' && seriesError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {seriesError}
        </div>
      )}

      {/* 评价图表：IC / 分层 / 衰减 */}
      {tab === 'eval' && evalSeries && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="IC 序列与累计 IC">
            {icOption
              ? <Chart option={icOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="分组年化收益" meta={`单调性 = ${evalSeries.quantile.monotonicity ?? '—'}`}>
            {groupOption
              ? <Chart option={groupOption} height={260} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="分层净值曲线" meta={`${evalSeries.n_groups} 组 + 多空`}>
            {quantileOption
              ? <Chart option={quantileOption} height={280} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel title="超额净值曲线" meta={`相对${evalSeries.excess?.benchmark ?? '股票池等权'}基准 · 稳定上行 = 真超额`}>
            {excessOption
              ? <Chart option={excessOption} height={280} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <Panel
            title="事件式分层收益"
            meta={evalSeries.event_study?.rel_periods.length
              ? `事件日 ±${evalSeries.event_study.after} 日 · 事前/事后发散比 ${
                evalSeries.event_study.look_ahead_ratio?.toFixed(2) ?? '—'}（<1 才是预测信号）`
              : '事件日前后各 N 日'}
          >
            {eventOption
              ? <Chart option={eventOption} height={280} />
              : <Empty>样本不足</Empty>}
          </Panel>
          <div className="space-y-5">
            <NeutralLadderPanel
              rows={evalSeries.neutral_ladder}
              returnNeutralIc={evalSeries.neutral_views?.return_neutral_ic}
            />
            <NeutralViewsSection views={evalSeries.neutral_views} />
            <GroupIcSection groupIc={evalSeries.group_ic} />
            <StyleCorrPanel styleCorr={evalSeries.style_corr} />
            <Panel title="IC 衰减" meta={`半衰期 ${evalRes?.half_life ?? '—'} 天 → 建议 ${evalRes?.suggested_rebalance ?? '—'}`}>
              {decayOption
                ? <Chart option={decayOption} height={180} />
                : <Empty>样本不足</Empty>}
            </Panel>
            <Panel title="分年度 IC" meta="突降 = 因子反转预警">
              {icYearOption
                ? <Chart option={icYearOption} height={180} />
                : <Empty>样本不足</Empty>}
            </Panel>
            <Panel title={`滚动窗口指标`} meta="掉头向下/转负 = 阶段性失效预警">
              {rollingOption
                ? <Chart option={rollingOption} height={180} />
                : <Empty>样本不足（需要 ≥ 60 个交易日）</Empty>}
            </Panel>
            <Panel title="滚动窗口 IR">
              {rollingOption
                ? <Chart option={{
                    tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
                    grid: { left: 48, right: 20, top: 20, bottom: 24 },
                    dataZoom: [{ type: 'inner' as const }],
                    ...axes({ data: evalSeries.rolling?.dates ?? [] }, { name: 'IR' }),
                    series: [{
                      name: '滚动IR', type: 'line' as const, data: evalSeries.rolling?.ir ?? [],
                      showSymbol: false, lineStyle: { width: 1.5, color: C.inkDim }, itemStyle: { color: C.inkDim },
                    }],
                  }} height={180} />
                : <Empty>样本不足</Empty>}
            </Panel>
          </div>
        </div>
      )}

      {/* 相关性 + 合成（lab Tab） */}
      {tab === 'lab' && (
      <Panel
        title="相关性 / 合成"
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={analyze}
              disabled={busy === 'corr' || picked.length < 2}
              className="btn btn-sm"
            >
              {busy === 'corr' ? '分析中…' : '分析相关性'}
            </button>
            <button
              onClick={() => doSynthesize('equal')}
              disabled={busy === 'syn' || picked.length < 2}
              className="btn btn-sm"
            >
              等权合成
            </button>
            <button
              onClick={() => doSynthesize('ic_weighted')}
              disabled={busy === 'syn' || picked.length < 2}
              className="btn btn-sm btn-accent"
            >
              IC 加权合成
            </button>
          </div>
        }
      >
        <div className="mb-4 flex flex-wrap gap-1">
          {picked.map((f) => (
            <button
              key={f}
              title="点击移除"
              onClick={() => toggle(f)}
              className={`tag ${FORMULAS.includes(f) ? 'tag-on' : ''}`}
            >
              {f} ×
            </button>
          ))}
        </div>

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <input
            value={customFormula}
            onChange={(e) => setCustomFormula(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') addCustom(); }}
            list="builtin-factors"
            placeholder="自定义公式 / DSL（如 $close/$open 或 CORR60），回车添加"
            className="input input-mono w-72 py-1 text-xs"
          />
          <datalist id="builtin-factors-lab">
            {(builtin ?? []).map((b) => <option key={b.name} value={b.name}>{b.formula}</option>)}
            {['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20', 'turnover']
              .map((f) => <option key={f} value={f} />)}
          </datalist>
          <button onClick={addCustom} disabled={!customFormula.trim()} className="btn btn-sm">
            添加
          </button>
          <span className="text-xs text-ink-faint">
            已选 {picked.length}/8 ·
          </span>
          <label className="flex items-center gap-1 text-xs text-ink-dim">
            起始日
            <input type="date" value={corrStart} onChange={(e) => setCorrStart(e.target.value)}
              className="input w-36 py-1 text-xs" />
          </label>
          <label className="flex items-center gap-1 text-xs text-ink-dim">
            结束日
            <input type="date" value={corrEnd} onChange={(e) => setCorrEnd(e.target.value)}
              className="input w-36 py-1 text-xs" />
          </label>
          <label className="flex items-center gap-1 text-xs text-ink-dim">
            股票池
            <select value={corrUniverse} onChange={(e) => setCorrUniverse(e.target.value)}
              className="input w-32 py-1 text-xs">
              {(universes ?? [{ key: 'all', index_code: null, label: '全市场' }]).map((u) => (
                <option key={u.key} value={u.key}>{u.label}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-xs text-ink-dim">
            冗余阈值
            <input type="number" min={0.5} max={1} step={0.05} value={corrThreshold}
              onChange={(e) => {
                // Number('') = 0，违反后端 ge=0.5 → 裸 422；空值回退默认并钳制
                const n = Number(e.target.value);
                setCorrThreshold(Number.isFinite(n) ? Math.min(1, Math.max(0.5, n)) : 0.8);
              }}
              className="input w-20 py-1 text-xs" />
          </label>
        </div>

        <div className="mb-4 flex flex-wrap gap-1">
          {FORMULAS.map((f) => (
            <button
              key={f}
              onClick={() => toggle(f)}
              className={`tag ${picked.includes(f) ? 'tag-on' : ''}`}
            >
              {f}
            </button>
          ))}
        </div>

        {corr && (
          <div className="mb-4 overflow-x-auto">
            <div className="mb-1 text-xs text-ink-faint">
              横截面 Spearman 相关（{corr.n_dates} 日均值）· |ρ|≥{corrThreshold} 判冗余
            </div>
            <table className="text-xs">
              <thead>
                <tr>
                  <th className="p-1" />
                  {corr.factors.map((f) => <th key={f} className="p-1 font-normal">{f}</th>)}
                </tr>
              </thead>
              <tbody>
                {corr.matrix.map((row, i) => (
                  <tr key={corr.factors[i]}>
                    <td className="p-1 font-medium">{corr.factors[i]}</td>
                    {row.map((v, j) => (
                      <td key={j} className="p-1 text-center">
                        <span
                          className="inline-block w-14 rounded-[2px] px-1 py-0.5 tabular-nums"
                          style={{ background: i === j ? '#ECECE6' : corrColor(v), color: i === j ? C.ink : '#fff' }}
                        >
                          {v.toFixed(2)}
                        </span>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {corr.redundant_pairs.length > 0 && (
              <div className="mt-2 text-xs text-gold">
                ⚠ 冗余对：{corr.redundant_pairs.map((p) => `${p.a}~${p.b}(${p.corr})`).join('、')} —— 不建议同时入库
              </div>
            )}
          </div>
        )}

        {syn && (
          <div className="border border-line bg-paper p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[13px] font-semibold">合成因子 {syn.factor}</span>
              <a href={syn.report_url} target="_blank" className="text-sm text-indigo hover:underline" rel="noreferrer">
                完整报告 ↗
              </a>
            </div>
            <div className="grid grid-cols-2 gap-y-3 divide-line md:grid-cols-5 md:divide-x">
              <div className="pr-4">
                <Stat label="IC 均值" value={syn.ic.mean ?? '—'}
                  tone={(syn.ic.mean ?? 0) > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="ICIR" value={syn.ic.ir ?? '—'} />
              </div>
              <div className="px-4">
                <Stat label="多空年化"
                  value={syn.long_short.annual_return != null
                    ? `${(syn.long_short.annual_return * 100).toFixed(1)}%` : '—'}
                  tone={(syn.long_short.annual_return ?? 0) > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="夏普" value={syn.long_short.sharpe ?? '—'} />
              </div>
              <div className="px-4">
                <Stat label="样本" value={syn.n_samples.toLocaleString()} />
              </div>
            </div>
          </div>
        )}
      </Panel>
      )}

      {tab === 'library' && (
      <FactorLibrary
        factors={factors}
        mutate={mutate}
        onPickFormula={(n) => { setFormula(n); setTab('eval'); }}
      />
      )}

      {tab === 'qlib' && <QlibWorkflowPanel />}
    </div>
  );
}
