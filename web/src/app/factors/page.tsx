'use client';

/**
 * 因子研究 —— 「研报台」版式：
 * 注册(DSL) + 快速评价 → 评价结果指标条 → IC/分层/衰减图表 → 相关性/合成 → 已注册因子。
 * 数据逻辑与旧版一致；evalSeries 为图表数据包（评价成功后拉取，失败不阻塞主结果）。
 */

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, Msg } from '@/components/States';
import { get, post } from '@/lib/api';
import { C, SERIES_COLORS, axes, legend, tooltip } from '@/lib/chart';

type FactorRow = { name: string; expression: string; description: string; created_at: string };
type EvalResult = {
  factor: string;
  n_samples: number;
  ic: { mean: number; ir: number; t_stat: number; positive_rate: number };
  rank_ic_mean: number;
  long_short: { annual_return: number; sharpe: number; max_drawdown: number };
  monotonicity: number;
  half_life: number | null;
  suggested_rebalance: string;
  report_url: string;
};
type EvalSeries = {
  factor: string; formula: string; n_groups: number; n_samples: number;
  ic: { dates: string[]; ic: (number | null)[]; rank_ic: (number | null)[]; cum_ic: number[] };
  quantile: {
    dates: string[];
    curves: Record<string, (number | null)[]>;
    groups: { q: number; annual_return: number | null; sharpe: number | null; mean_ret: number | null }[];
    monotonicity: number | null;
  };
  decay: { horizons: number[]; ic: (number | null)[]; rank_ic: (number | null)[] };
  ic_by_year: { year: number; ic_mean: number | null; ir: number | null; positive_rate: number | null }[];
  neutral_ladder?: { label: string; covs: string[]; ic_mean: number | null; rank_ic_mean: number | null; n_days: number }[];
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

/** 分层净值用色：靛青系为主，多空单独朱砂 */
const Q_COLORS = ['#94989F', '#31589E', '#4E6E8E', '#3E8E7E', '#B08A3E', '#A85B4B', '#6B4F9E', '#C3352B', '#1E7C55', '#2F5D4E'];

function corrColor(v: number): string {
  const a = Math.min(Math.abs(v), 1);
  const blue = `rgba(49,88,158,${(a * 0.75).toFixed(2)})`;   // 靛 · 正相关
  const red = `rgba(195,53,43,${(a * 0.75).toFixed(2)})`;    // 朱 · 负相关
  return v >= 0 ? blue : red;
}

export default function FactorsPage() {
  const { data: factors, mutate } = useSWR<FactorRow[]>('/factors', get);
  const [srcFilter, setSrcFilter] = useState<string | null>(null);
  const shownFactors = (factors ?? []).filter(
    (f) => !srcFilter || (f as unknown as { source?: string }).source === srcFilter);
  const { data: builtin } = useSWR<BuiltinItem[]>('/factors/builtin', get);
  const [name, setName] = useState('mom20');
  const [expression, setExpression] = useState('Rank(Ts_Mean($close,5)/$close-1)');
  const [formula, setFormula] = useState('pct_change_20');
  const [evalRes, setEvalRes] = useState<EvalResult | null>(null);
  const [evalSeries, setEvalSeries] = useState<EvalSeries | null>(null);
  const [seriesError, setSeriesError] = useState<string | null>(null);
  const [busy, setBusy] = useState<'' | 'reg' | 'eval' | 'corr' | 'syn' | 'seed'>('');
  const [msg, setMsg] = useState('');
  // 相关性 / 合成
  const [picked, setPicked] = useState<string[]>(['pct_change_20', 'rolling_std_20']);
  const [corr, setCorr] = useState<CorrResult | null>(null);
  const [syn, setSyn] = useState<SynResult | null>(null);
  const [builtinQuery, setBuiltinQuery] = useState('');

  // 内置因子按族浏览 + 搜索（158 个）
  const builtinShown = useMemo(() => {
    if (!builtin) return [];
    const q = builtinQuery.trim().toUpperCase();
    return q ? builtin.filter((b) => b.name.includes(q)) : builtin.slice(0, 40);
  }, [builtin, builtinQuery]);

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
            color: (p: { data: number | null }) => (p.data == null || p.data >= 0 ? C.up : C.down),
          },
        },
        { name: '累计IC', type: 'line' as const, yAxisIndex: 1, data: evalSeries.ic.cum_ic, showSymbol: false, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
      ],
    };
  }, [evalSeries]);

  const quantileOption = useMemo(() => {
    if (!evalSeries?.quantile.dates.length) return null;
    const { dates, curves } = evalSeries.quantile;
    const keys = Object.keys(curves).filter((k) => k !== 'long_short')
      .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => v?.toFixed(3) },
      legend: legend({ top: 0, data: [...keys, '多空'] }),
      grid: { left: 48, right: 20, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      ...axes({ data: dates }, { scale: true, name: '净值' }),
      series: [
        ...keys.map((k, i) => ({
          name: `第${k.slice(1)}组`, type: 'line' as const, data: curves[k],
          showSymbol: false, lineStyle: { width: 1, color: Q_COLORS[i % Q_COLORS.length] },
          itemStyle: { color: Q_COLORS[i % Q_COLORS.length] },
        })),
        {
          name: '多空', type: 'line' as const, data: curves.long_short, showSymbol: false,
          lineStyle: { width: 2.5, color: C.up }, itemStyle: { color: C.up }, z: 5,
        },
      ],
    };
  }, [evalSeries]);

  const groupOption = useMemo(() => {
    if (!evalSeries?.quantile.groups.length) return null;
    const gs = evalSeries.quantile.groups;
    return {
      tooltip: { ...tooltip, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 56, right: 16, top: 24, bottom: 24 },
      ...axes({ data: gs.map((g) => `Q${g.q}`) },
        { axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } }),
      series: [{
        name: '年化收益', type: 'bar' as const,
        data: gs.map((g) => ({
          value: g.annual_return,
          itemStyle: { color: (g.annual_return ?? 0) >= 0 ? C.up : C.down },
        })),
        barMaxWidth: 36,
      }],
    };
  }, [evalSeries]);

  const decayOption = useMemo(() => {
    if (!evalSeries?.decay.horizons.length) return null;
    const { horizons, ic, rank_ic } = evalSeries.decay;
    return {
      tooltip,
      legend: legend({ top: 0 }),
      grid: { left: 48, right: 20, top: 30, bottom: 24 },
      ...axes({ data: horizons.map((h) => `${h}天`) }, { name: 'IC均值' }),
      series: [
        { name: 'IC', type: 'line' as const, data: ic, showSymbol: true, lineStyle: { width: 1.8, color: C.up }, itemStyle: { color: C.up } },
        { name: 'RankIC', type: 'line' as const, data: rank_ic, showSymbol: true, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
      ],
    };
  }, [evalSeries]);

  const icYearOption = useMemo(() => {
    if (!evalSeries?.ic_by_year.length) return null;
    const ys = evalSeries.ic_by_year;
    return {
      tooltip,
      grid: { left: 48, right: 16, top: 24, bottom: 24 },
      ...axes({ data: ys.map((y) => String(y.year)) }, { name: 'IC均值' }),
      series: [{
        name: '分年度IC', type: 'bar' as const,
        data: ys.map((y) => ({
          value: y.ic_mean,
          itemStyle: { color: (y.ic_mean ?? 0) >= 0 ? C.up : C.down },
        })),
        barMaxWidth: 32,
      }],
    };
  }, [evalSeries]);

  function toggle(f: string) {
    setPicked((p) => (p.includes(f) ? p.filter((x) => x !== f) : [...p, f]));
  }

  async function register() {
    setBusy('reg');
    setMsg('');
    try {
      await post('/factors', { name, expression, description: '' });
      setMsg('✓ 已注册');
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function evaluate() {
    setBusy('eval');
    setMsg('');
    try {
      const params = { factor: name || 'tmp', formula, n_groups: 5 };
      const r = await post<EvalResult & { series?: EvalSeries }>('/factors/evaluate', params);
      setEvalRes(r);
      // 图表数据包随主评价一次返回（后端已合并计算）
      setEvalSeries(r.series ?? null);
      setSeriesError(r.series ? null : '图表数据缺失：评价响应未包含 series 字段');
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function seedBuiltin() {
    setBusy('seed');
    setMsg('');
    try {
      const r = await post<{ seeded: number }>('/factors/seed-builtin', {});
      setMsg(`✓ 已入库 ${r.seeded} 个 Qlib 内置因子`);
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function analyze() {
    setBusy('corr');
    setMsg('');
    try {
      setCorr(await post<CorrResult>('/factors/analyze', { formulas: picked }));
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
      setSyn(await post<SynResult>('/factors/synthesize', { formulas: picked, method }));
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
        sub={<>注册 DSL · 快速评价（IC / 分层 / 衰减）· 相关性与合成{builtin ? ` · 内置 Qlib Alpha158 ${builtin.length} 个` : ''}</>}
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="注册因子（DSL）">
          <div className="space-y-3">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="因子名"
              className="input w-full"
            />
            <textarea
              value={expression}
              onChange={(e) => setExpression(e.target.value)}
              rows={3}
              placeholder="Rank(Ts_Mean($close,5)/$close-1)"
              className="input input-mono w-full"
            />
            <button
              onClick={register}
              disabled={busy === 'reg' || !name}
              className="btn btn-primary"
            >
              {busy === 'reg' ? '注册中…' : '注册（AST 校验）'}
            </button>
          </div>
        </Panel>

        <Panel title="快速评价" meta="IC / 分层 / 衰减 · 一次跑完">
          <div className="space-y-3">
            <input
              value={formula}
              onChange={(e) => setFormula(e.target.value)}
              list="builtin-factors"
              placeholder="MA20 / RSV10 / ROC5 / KMID / pct_change_20 …"
              className="input input-mono w-full"
            />
            <datalist id="builtin-factors">
              {(builtin ?? []).map((b) => <option key={b.name} value={b.name}>{b.formula}</option>)}
            </datalist>
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
            <button
              onClick={evaluate}
              disabled={busy === 'eval' || !formula}
              className="btn btn-accent"
            >
              {busy === 'eval' ? '评价中…' : '运行评价'}
            </button>
            <p className="text-xs text-ink-faint">
              输入任意内置因子名（如 BETA20、CORR60）或传统公式（pct_change_n / rolling_std_n / turnover）。
            </p>
          </div>
        </Panel>
      </div>

      <Msg text={msg} />

      {evalRes && (
        <Panel
          title="评价结果"
          meta={<>{evalRes.factor} · 样本 {evalRes.n_samples}</>}
          actions={
            <a
              href={evalRes.report_url}
              target="_blank"
              className="text-sm text-indigo hover:underline"
              rel="noreferrer"
            >
              查看完整报告 ↗
            </a>
          }
        >
          <div className="grid grid-cols-2 gap-y-4 divide-line md:grid-cols-4 lg:grid-cols-8 md:divide-x">
            <div className="pr-4">
              <Stat label="IC 均值" value={evalRes.ic.mean}
                tone={evalRes.ic.mean > 0 ? 'text-up' : 'text-down'} />
            </div>
            <div className="px-4">
              <Stat label="ICIR" value={evalRes.ic.ir} />
            </div>
            <div className="px-4">
              <Stat label="t 统计量" value={evalRes.ic.t_stat} />
            </div>
            <div className="px-4">
              <Stat label="多空年化" value={`${(evalRes.long_short.annual_return * 100).toFixed(2)}%`}
                tone={evalRes.long_short.annual_return > 0 ? 'text-up' : 'text-down'} />
            </div>
            <div className="px-4">
              <Stat label="多空夏普" value={evalRes.long_short.sharpe} />
            </div>
            <div className="px-4">
              <Stat label="单调性" value={evalRes.monotonicity} />
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

      {/* 图表加载失败显式提示（缺陷 #4：不再伪装成「样本不足」） */}
      {seriesError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {seriesError}
        </div>
      )}

      {/* 评价图表：IC / 分层 / 衰减 */}
      {evalSeries && (
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
          <div className="space-y-5">
            <Panel title="IC 归因阶梯" meta="原始 → +市值 → +行业 → +换手率（逐段叠加看 IC 掉多少）">
            {(evalSeries.neutral_ladder?.length ?? 0) > 0 ? (
              <div className="space-y-1.5 px-1 py-2 text-xs">
                {evalSeries.neutral_ladder!.map((l) => {
                  const cov = (l as { coverage?: number }).coverage ?? 1;
                  const dim = cov < 0.8;
                  const first = evalSeries.neutral_ladder![0].ic_mean ?? 0;
                  const v = l.ic_mean ?? 0;
                  const drop = first !== 0 ? ((first - v) / Math.abs(first) * 100).toFixed(0) : '0';
                  const w = first !== 0 ? Math.min(Math.abs(v / first) * 100, 100) : 0;
                  return (
                    <div key={l.label}
                      className={`flex items-center gap-2 rounded-sm px-1 ${dim ? 'bg-ink-faint/10 opacity-60' : ''}`}
                      title={dim ? `协变量覆盖率 ${(cov * 100).toFixed(0)}% < 80%` : ''}>
                      <span className="w-24 text-ink-dim">{l.label}</span>
                      <div className="h-3 flex-1 rounded-sm bg-ink-faint/10">
                        <div className="h-3 rounded-sm" style={{ width: `${w}%`, background: 'var(--c-indigo, #31589E)' }} />
                      </div>
                      <span className="w-16 text-right font-mono">{v.toFixed(4)}</span>
                      <span className="w-10 text-right text-ink-faint">↓{drop}%</span>
                    </div>
                  );
                })}
              </div>
            ) : <Empty>协变量数据不足</Empty>}
          </Panel>
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
          </div>
        </div>
      )}

      {/* 相关性 + 合成 */}
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
              横截面 Spearman 相关（{corr.n_dates} 日均值）· |ρ|≥0.8 判冗余
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
                <Stat label="IC 均值" value={syn.ic.mean} tone={syn.ic.mean > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="ICIR" value={syn.ic.ir} />
              </div>
              <div className="px-4">
                <Stat label="多空年化" value={`${(syn.long_short.annual_return * 100).toFixed(1)}%`}
                  tone={syn.long_short.annual_return > 0 ? 'text-up' : 'text-down'} />
              </div>
              <div className="px-4">
                <Stat label="夏普" value={syn.long_short.sharpe} />
              </div>
              <div className="px-4">
                <Stat label="样本" value={syn.n_samples.toLocaleString()} />
              </div>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="已注册因子"
        meta={<>共 {shownFactors.length} 个 · Qlib Alpha158 内置因子可一键入库</>}
        actions={
          <button
            onClick={seedBuiltin}
            disabled={busy === 'seed'}
            className="btn btn-sm"
          >
            {busy === 'seed' ? '入库中…' : '一键入库内置因子'}
          </button>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-1">
          {['全部', 'qlib', 'yaml', 'manual'].map((src) => (
            <button
              key={src}
              onClick={() => setSrcFilter(src === '全部' ? null : src)}
              className={`tag ${(srcFilter ?? '全部') === src ? 'tag-on' : ''}`}
            >
              {src}
            </button>
          ))}
        </div>
        <div className="mb-4 flex flex-wrap items-center gap-1">
          <input
            value={builtinQuery}
            onChange={(e) => setBuiltinQuery(e.target.value)}
            placeholder="搜索内置因子，如 RSV / CORR / STD20"
            className="input input-mono w-64 py-1 text-xs"
          />
          {builtinShown.slice(0, 24).map((b) => (
            <button
              key={b.name}
              title={b.formula}
              onClick={() => setFormula(b.name)}
              className="tag"
            >
              {b.name}
            </button>
          ))}
        </div>
        {!factors?.length ? (
          <Empty>暂无 —— 用上方表单注册第一个因子</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">名称</th>
                <th className="text-left">表达式</th>
                <th className="text-left">来源</th>
                <th className="text-left">IC(中性化)</th>
                <th className="text-left">注册时间</th>
              </tr>
            </thead>
            <tbody>
              {shownFactors.map((f) => {
                const src = (f as unknown as { source?: string }).source;
                const icn = (f as unknown as { ic_neutral?: number | null }).ic_neutral;
                const icnDisplay = icn == null ? '—' : Number(icn).toFixed(4);
                return (
                  <tr key={f.name} className="hover:bg-white">
                    <td className="font-medium">
                      <a href={`/factors/${f.name}`} className="hover:underline">{f.name}</a>
                    </td>
                    <td className="font-mono text-xs text-ink-dim">{f.expression || '—'}</td>
                    <td className="text-ink-faint">{src ?? 'manual'}</td>
                    <td className="font-mono">{icnDisplay}</td>
                    <td className="text-ink-faint">{f.created_at?.slice(0, 19)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
