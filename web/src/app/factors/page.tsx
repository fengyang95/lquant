'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import ReactECharts from 'echarts-for-react';
import { get, post } from '@/lib/api';

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

const Q_COLORS = ['#94a3b8', '#60a5fa', '#2563eb', '#7c3aed', '#db2777', '#ea580c', '#e5484d', '#16a34a', '#059669', '#065f46'];

function corrColor(v: number): string {
  const a = Math.min(Math.abs(v), 1);
  const blue = `rgba(37,99,235,${(a * 0.75).toFixed(2)})`;
  const red = `rgba(229,72,77,${(a * 0.75).toFixed(2)})`;
  return v >= 0 ? blue : red;
}

export default function FactorsPage() {
  const { data: factors, mutate } = useSWR<FactorRow[]>('/factors', get);
  const { data: builtin } = useSWR<BuiltinItem[]>('/factors/builtin', get);
  const [name, setName] = useState('mom20');
  const [expression, setExpression] = useState('Rank(Ts_Mean($close,5)/$close-1)');
  const [formula, setFormula] = useState('pct_change_20');
  const [evalRes, setEvalRes] = useState<EvalResult | null>(null);
  const [evalSeries, setEvalSeries] = useState<EvalSeries | null>(null);
  const [busy, setBusy] = useState<'' | 'reg' | 'eval' | 'corr' | 'syn' | 'seed'>('');
  const [msg, setMsg] = useState('');
  // F6/F7
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
      tooltip: { trigger: 'axis' as const },
      legend: { top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 48, right: 48, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      xAxis: { type: 'category' as const, data: evalSeries.ic.dates, axisLabel: { fontSize: 10 } },
      yAxis: [
        { type: 'value' as const, name: 'IC' },
        { type: 'value' as const, name: '累计IC' },
      ],
      series: [
        {
          name: '日度IC', type: 'bar' as const, data: evalSeries.ic.ic,
          itemStyle: {
            color: (p: { data: number | null }) => (p.data == null || p.data >= 0 ? '#e5484d' : '#16a34a'),
          },
        },
        { name: '累计IC', type: 'line' as const, yAxisIndex: 1, data: evalSeries.ic.cum_ic, showSymbol: false, lineStyle: { width: 1.5, color: '#2563eb' }, itemStyle: { color: '#2563eb' } },
      ],
    };
  }, [evalSeries]);

  const quantileOption = useMemo(() => {
    if (!evalSeries?.quantile.dates.length) return null;
    const { dates, curves } = evalSeries.quantile;
    const keys = Object.keys(curves).filter((k) => k !== 'long_short')
      .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
    return {
      tooltip: { trigger: 'axis' as const, valueFormatter: (v: number) => v?.toFixed(3) },
      legend: { top: 0, textStyle: { fontSize: 10 }, data: [...keys, '多空'] },
      grid: { left: 48, right: 20, top: 30, bottom: 24 },
      dataZoom: [{ type: 'inside' as const }],
      xAxis: { type: 'category' as const, data: dates, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value' as const, scale: true, name: '净值' },
      series: [
        ...keys.map((k, i) => ({
          name: `第${k.slice(1)}组`, type: 'line' as const, data: curves[k],
          showSymbol: false, lineStyle: { width: 1, color: Q_COLORS[i % Q_COLORS.length] },
          itemStyle: { color: Q_COLORS[i % Q_COLORS.length] },
        })),
        {
          name: '多空', type: 'line' as const, data: curves.long_short, showSymbol: false,
          lineStyle: { width: 2.5, color: '#dc2626' }, itemStyle: { color: '#dc2626' }, z: 5,
        },
      ],
    };
  }, [evalSeries]);

  const groupOption = useMemo(() => {
    if (!evalSeries?.quantile.groups.length) return null;
    const gs = evalSeries.quantile.groups;
    return {
      tooltip: { trigger: 'axis' as const, valueFormatter: (v: number) => `${(v * 100).toFixed(2)}%` },
      grid: { left: 56, right: 16, top: 24, bottom: 24 },
      xAxis: { type: 'category' as const, data: gs.map((g) => `Q${g.q}`) },
      yAxis: { type: 'value' as const, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: [{
        name: '年化收益', type: 'bar' as const,
        data: gs.map((g) => ({
          value: g.annual_return,
          itemStyle: { color: (g.annual_return ?? 0) >= 0 ? '#e5484d' : '#16a34a' },
        })),
        barMaxWidth: 36,
      }],
    };
  }, [evalSeries]);

  const decayOption = useMemo(() => {
    if (!evalSeries?.decay.horizons.length) return null;
    const { horizons, ic, rank_ic } = evalSeries.decay;
    return {
      tooltip: { trigger: 'axis' as const },
      legend: { top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 48, right: 20, top: 30, bottom: 24 },
      xAxis: { type: 'category' as const, data: horizons.map((h) => `${h}天`) },
      yAxis: { type: 'value' as const, name: 'IC均值' },
      series: [
        { name: 'IC', type: 'line' as const, data: ic, showSymbol: true, lineStyle: { width: 1.8, color: '#dc2626' }, itemStyle: { color: '#dc2626' } },
        { name: 'RankIC', type: 'line' as const, data: rank_ic, showSymbol: true, lineStyle: { width: 1.5, color: '#2563eb' }, itemStyle: { color: '#2563eb' } },
      ],
    };
  }, [evalSeries]);

  const icYearOption = useMemo(() => {
    if (!evalSeries?.ic_by_year.length) return null;
    const ys = evalSeries.ic_by_year;
    return {
      tooltip: { trigger: 'axis' as const },
      grid: { left: 48, right: 16, top: 24, bottom: 24 },
      xAxis: { type: 'category' as const, data: ys.map((y) => String(y.year)) },
      yAxis: { type: 'value' as const, name: 'IC均值' },
      series: [{
        name: '分年度IC', type: 'bar' as const,
        data: ys.map((y) => ({
          value: y.ic_mean,
          itemStyle: { color: (y.ic_mean ?? 0) >= 0 ? '#e5484d' : '#16a34a' },
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
      const r = await post<EvalResult>('/factors/evaluate', params);
      setEvalRes(r);
      // 图表数据包（失败不阻塞主评价结果）
      try {
        setEvalSeries(await post<EvalSeries>('/factors/evaluate/series', params));
      } catch { setEvalSeries(null); }
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
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">因子研究</h1>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3 rounded-xl border bg-white p-4">
          <div className="text-sm font-medium">注册因子（DSL）</div>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="因子名"
            className="w-full rounded-md border px-3 py-2 text-sm"
          />
          <textarea
            value={expression}
            onChange={(e) => setExpression(e.target.value)}
            rows={3}
            placeholder="Rank(Ts_Mean($close,5)/$close-1)"
            className="w-full rounded-md border px-3 py-2 font-mono text-sm"
          />
          <button
            onClick={register}
            disabled={busy === 'reg' || !name}
            className="rounded-md bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700 disabled:opacity-40"
          >
            {busy === 'reg' ? '注册中…' : '注册（AST 校验）'}
          </button>
        </div>

        <div className="space-y-3 rounded-xl border bg-white p-4">
          <div className="text-sm font-medium">
            快速评价（IC/分层/衰减）
            <span className="ml-1 text-xs text-neutral-400">内置 Qlib Alpha158 · 158 个</span>
          </div>
          <input
            value={formula}
            onChange={(e) => setFormula(e.target.value)}
            list="builtin-factors"
            placeholder="MA20 / RSV10 / ROC5 / KMID / pct_change_20 …"
            className="w-full rounded-md border px-3 py-2 font-mono text-sm"
          />
          <datalist id="builtin-factors">
            {(builtin ?? []).map((b) => <option key={b.name} value={b.name}>{b.formula}</option>)}
          </datalist>
          <div className="flex flex-wrap gap-1">
            {FORMULAS.slice(5).map((f) => (
              <button
                key={f}
                onClick={() => setFormula(f)}
                className={`rounded-full px-2 py-0.5 text-xs transition ${
                  formula === f ? 'bg-red-600 text-white' : 'border text-neutral-500 hover:bg-neutral-100'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
          <button
            onClick={evaluate}
            disabled={busy === 'eval' || !formula}
            className="rounded-md bg-red-600 px-4 py-2 text-sm text-white hover:bg-red-500 disabled:opacity-40"
          >
            {busy === 'eval' ? '评价中…' : '运行评价'}
          </button>
          <p className="text-xs text-neutral-400">
            输入任意内置因子名（如 BETA20、CORR60）或传统公式（pct_change_n / rolling_std_n / turnover）。
          </p>
        </div>
      </div>

      {msg && <div className="rounded-md border bg-white px-4 py-2 text-sm">{msg}</div>}

      {evalRes && (
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-sm font-medium">
              评价结果 · {evalRes.factor} <span className="text-neutral-400">（样本 {evalRes.n_samples}）</span>
            </span>
            <a
              href={evalRes.report_url}
              target="_blank"
              className="text-sm text-blue-600 hover:underline"
              rel="noreferrer"
            >
              查看完整报告 ↗
            </a>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <div>
              <div className="text-xs text-neutral-400">IC 均值</div>
              <div className={`font-semibold tabular-nums ${evalRes.ic.mean > 0 ? 'text-up' : 'text-down'}`}>
                {evalRes.ic.mean}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">ICIR</div>
              <div className="font-semibold tabular-nums">{evalRes.ic.ir}</div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">t 统计量</div>
              <div className="font-semibold tabular-nums">{evalRes.ic.t_stat}</div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">多空年化</div>
              <div className={`font-semibold tabular-nums ${evalRes.long_short.annual_return > 0 ? 'text-up' : 'text-down'}`}>
                {(evalRes.long_short.annual_return * 100).toFixed(2)}%
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">多空夏普</div>
              <div className="font-semibold tabular-nums">{evalRes.long_short.sharpe}</div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">单调性</div>
              <div className="font-semibold tabular-nums">{evalRes.monotonicity}</div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">半衰期</div>
              <div className="font-semibold tabular-nums">
                {evalRes.half_life ?? '—'} <span className="text-xs text-neutral-400">天</span>
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">建议调仓</div>
              <div className="font-semibold">{evalRes.suggested_rebalance}</div>
            </div>
          </div>
        </div>
      )}

      {/* 评价图表：IC / 分层 / 衰减 */}
      {evalSeries && (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-medium">IC 序列与累计 IC</div>
              {icOption
                ? <ReactECharts option={icOption} style={{ height: 260 }} notMerge />
                : <div className="py-16 text-center text-sm text-neutral-400">样本不足</div>}
            </div>
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-medium">
                分组年化收益（单调性 = {evalSeries.quantile.monotonicity ?? '—'}）
              </div>
              {groupOption
                ? <ReactECharts option={groupOption} style={{ height: 260 }} notMerge />
                : <div className="py-16 text-center text-sm text-neutral-400">样本不足</div>}
            </div>
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-medium">
                分层净值曲线（{evalSeries.n_groups} 组 + 多空）
              </div>
              {quantileOption
                ? <ReactECharts option={quantileOption} style={{ height: 280 }} notMerge />
                : <div className="py-16 text-center text-sm text-neutral-400">样本不足</div>}
            </div>
            <div className="space-y-4">
              <div className="rounded-xl border bg-white p-4">
                <div className="mb-2 text-sm font-medium">
                  IC 衰减（半衰期 {evalRes?.half_life ?? '—'} 天 → 建议 {evalRes?.suggested_rebalance ?? '—'}）
                </div>
                {decayOption
                  ? <ReactECharts option={decayOption} style={{ height: 180 }} notMerge />
                  : <div className="py-8 text-center text-sm text-neutral-400">样本不足</div>}
              </div>
              <div className="rounded-xl border bg-white p-4">
                <div className="mb-2 text-sm font-medium">分年度 IC（突降 = 因子反转预警）</div>
                {icYearOption
                  ? <ReactECharts option={icYearOption} style={{ height: 180 }} notMerge />
                  : <div className="py-8 text-center text-sm text-neutral-400">样本不足</div>}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* F6 相关性 + F7 合成 */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">相关性 / 合成</span>
          {FORMULAS.map((f) => (
            <button
              key={f}
              onClick={() => toggle(f)}
              className={`rounded-full px-3 py-1 text-xs transition ${
                picked.includes(f)
                  ? 'bg-neutral-900 text-white'
                  : 'border text-neutral-500 hover:bg-neutral-100'
              }`}
            >
              {f}
            </button>
          ))}
          <button
            onClick={analyze}
            disabled={busy === 'corr' || picked.length < 2}
            className="ml-auto rounded-md border px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-40"
          >
            {busy === 'corr' ? '分析中…' : '分析相关性'}
          </button>
          <button
            onClick={() => doSynthesize('equal')}
            disabled={busy === 'syn' || picked.length < 2}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-40"
          >
            等权合成
          </button>
          <button
            onClick={() => doSynthesize('ic_weighted')}
            disabled={busy === 'syn' || picked.length < 2}
            className="rounded-md bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-500 disabled:opacity-40"
          >
            IC 加权合成
          </button>
        </div>

        {corr && (
          <div className="mb-3 overflow-x-auto">
            <div className="mb-1 text-xs text-neutral-400">
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
                          className="inline-block w-14 rounded px-1 py-0.5 tabular-nums"
                          style={{ background: i === j ? '#f5f5f5' : corrColor(v), color: i === j ? '#333' : '#fff' }}
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
              <div className="mt-2 text-xs text-orange-600">
                ⚠ 冗余对：{corr.redundant_pairs.map((p) => `${p.a}~${p.b}(${p.corr})`).join('、')} —— 不建议同时入库
              </div>
            )}
          </div>
        )}

        {syn && (
          <div className="rounded-lg border bg-neutral-50 p-3 text-sm">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-medium">合成因子 {syn.factor}</span>
              <a href={syn.report_url} target="_blank" className="text-blue-600 hover:underline" rel="noreferrer">
                完整报告 ↗
              </a>
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
              <div><span className="text-xs text-neutral-400">IC 均值</span><div className={`font-semibold ${syn.ic.mean > 0 ? 'text-up' : 'text-down'}`}>{syn.ic.mean}</div></div>
              <div><span className="text-xs text-neutral-400">ICIR</span><div className="font-semibold tabular-nums">{syn.ic.ir}</div></div>
              <div><span className="text-xs text-neutral-400">多空年化</span><div className={`font-semibold ${syn.long_short.annual_return > 0 ? 'text-up' : 'text-down'}`}>{(syn.long_short.annual_return * 100).toFixed(1)}%</div></div>
              <div><span className="text-xs text-neutral-400">夏普</span><div className="font-semibold tabular-nums">{syn.long_short.sharpe}</div></div>
              <div><span className="text-xs text-neutral-400">样本</span><div className="font-semibold tabular-nums">{syn.n_samples.toLocaleString()}</div></div>
            </div>
          </div>
        )}
      </div>

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm font-medium">
            已注册因子（{factors?.length ?? 0}）
            <span className="ml-2 text-xs text-neutral-400">Qlib Alpha158 内置因子可一键入库</span>
          </div>
          <button
            onClick={seedBuiltin}
            disabled={busy === 'seed'}
            className="rounded-md border px-3 py-1.5 text-xs hover:bg-neutral-100 disabled:opacity-40"
          >
            {busy === 'seed' ? '入库中…' : '一键入库内置因子'}
          </button>
        </div>
        <div className="mb-3 flex flex-wrap gap-1">
          <input
            value={builtinQuery}
            onChange={(e) => setBuiltinQuery(e.target.value)}
            placeholder="搜索内置因子，如 RSV / CORR / STD20"
            className="w-64 rounded-md border px-2 py-1 text-xs"
          />
          {builtinShown.slice(0, 24).map((b) => (
            <button
              key={b.name}
              title={b.formula}
              onClick={() => setFormula(b.name)}
              className="rounded border border-neutral-200 px-1.5 py-0.5 font-mono text-[11px] text-neutral-500 hover:border-red-400 hover:text-red-600"
            >
              {b.name}
            </button>
          ))}
        </div>
        {!factors?.length ? (
          <div className="py-6 text-center text-sm text-neutral-400">暂无 —— 用上方表单注册第一个因子</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-neutral-400">
              <tr className="border-b">
                <th className="py-1.5 text-left font-normal">名称</th>
                <th className="text-left font-normal">表达式</th>
                <th className="text-left font-normal">注册时间</th>
              </tr>
            </thead>
            <tbody>
              {factors.map((f) => (
                <tr key={f.name} className="border-b border-neutral-50">
                  <td className="py-1.5 font-medium">
                    <a href={`/factors/${f.name}`} className="hover:underline">{f.name}</a>
                  </td>
                  <td className="font-mono text-xs text-neutral-500">{f.expression || '—'}</td>
                  <td className="text-neutral-400">{f.created_at?.slice(0, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
