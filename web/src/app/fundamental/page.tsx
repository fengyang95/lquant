// 全市场基本面排名 —— 数据源 /fundamental/scores（行业相对分位 + 覆盖率）。
// 排名用归一化分数，但把覆盖率单列出来：低覆盖的高分不具备可比的可信度。
//
// 页面同时承载三件事，用页签分开：
//  1. 排名（全市场筛选 / 排序 / 搜索 / 导出）
//  2. 行业分位（解释分数是怎么来的）
//  3. 三表勾稽（独立于总分的质量检查）
// 后两者对应的后端端点此前完全没有前端接线。
'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import PercentileMatrix from '@/components/PercentileMatrix';
import ReconcilePanel from '@/components/ReconcilePanel';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { get, post } from '@/lib/api';

type ScoreRow = {
  symbol: string;
  industry: string | null;
  n_scored: number;
  n_metrics: number;
  coverage: number;
  raw_score: number;
  available_max: number;
  normalized_score: number;
  rating: string;
  [key: string]: unknown;
};

type DataSourceStat = {
  available: boolean;
  rows: number;
  hint?: string | null;
  lookback_years?: number;
};

type Availability = {
  financial_pit?: DataSourceStat;
  valuation_lake?: DataSourceStat;
};

type ModuleStat = {
  n_metrics: number;
  max_score: number;
  n_scored_rows: number;
  hit_rate: number;
};

type UniverseResp = {
  asof: string;
  available: boolean;
  hint?: string;
  n_scored: number;
  n_total?: number;
  rows: ScoreRow[];
  modules?: Record<string, ModuleStat>;
  availability?: Availability;
};

type ModuleMeta = { name: string; label: string; weight: number; n_metrics?: number };
type MetricMeta = {
  item: string; label: string; module: string; max_score: number;
  higher_better: boolean; direction: string; source?: string;
};
type MetricsResp = {
  modules: ModuleMeta[];
  metrics: MetricMeta[];
  sources?: Record<string, string>;
};

type IndustryRow = { industry: string; n: number; avg_score: number };

const RATING_TONE: Record<string, string> = {
  优秀: 'text-up',
  良好: 'text-indigo',
  一般: 'text-ink',
  较差: 'text-down',
};

const SOURCE_LABEL: Record<string, string> = {
  pit: '报表',
  derived: '派生',
  valuation: '估值',
};

/** 与后端 _SORTABLE 对齐，避免前端传一个后端会 422 的列名 */
const SORTABLE = new Set([
  'normalized_score', 'coverage', 'raw_score', 'available_max', 'n_scored', 'symbol',
]);

type Tab = 'rank' | 'percentile' | 'reconcile';

export default function FundamentalPage() {
  const [asof, setAsof] = useState('');
  const [minCoverage, setMinCoverage] = useState(0.5);
  const [limit, setLimit] = useState(50);
  const [minSamples, setMinSamples] = useState(5);
  const [industries, setIndustries] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [sortBy, setSortBy] = useState('normalized_score');
  const [sortDesc, setSortDesc] = useState(true);
  const [tab, setTab] = useState<Tab>('rank');
  const [debouncedSearch, setDebouncedSearch] = useState('');

  // 搜索防抖：后端一次全市场评分是重查询，不能每敲一个字符就打一次
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim().toUpperCase()), 300);
    return () => clearTimeout(t);
  }, [search]);

  const { data: catalogue } = useSWR<MetricsResp>('/fundamental/metrics', get);
  const { data: industryList } = useSWR<{ rows: IndustryRow[] }>(
    ['/fundamental/industries', asof, minSamples],
    () => get<{ rows: IndustryRow[] }>(
      `/fundamental/industries?min_samples=${minSamples}${asof ? `&asof=${asof}` : ''}`),
  );

  const key = ['/fundamental/scores', asof, minCoverage, limit, minSamples,
    industries.join(','), debouncedSearch, sortBy, sortDesc];
  const { data, isLoading, error } = useSWR<UniverseResp>(
    key,
    () => post<UniverseResp>('/fundamental/scores', {
      asof: asof || null,
      min_coverage: minCoverage,
      min_samples: minSamples,
      limit,
      industries,
      symbols: debouncedSearch ? [debouncedSearch] : [],
      sort_by: SORTABLE.has(sortBy) ? sortBy : 'normalized_score',
      sort_desc: sortDesc,
    }),
  );

  const modules = catalogue?.modules ?? [];
  const rows = data?.rows ?? [];
  const fin = data?.availability?.financial_pit;
  const val = data?.availability?.valuation_lake;

  function toggleSort(col: string) {
    if (sortBy === col) setSortDesc((d) => !d);
    else {
      setSortBy(col);
      setSortDesc(col !== 'symbol');
    }
  }

  function toggleIndustry(name: string) {
    setIndustries((cur) => (cur.includes(name)
      ? cur.filter((x) => x !== name)
      : [...cur, name]));
  }

  function reset() {
    setAsof('');
    setMinCoverage(0.5);
    setLimit(50);
    setMinSamples(5);
    setIndustries([]);
    setSearch('');
    setSortBy('normalized_score');
    setSortDesc(true);
  }

  function exportCsv() {
    const cols = ['symbol', 'industry', 'normalized_score', 'coverage', 'rating',
      'raw_score', 'available_max', 'n_scored', 'n_metrics',
      ...modules.map((m) => `score_${m.name}`)];
    const head = cols.join(',');
    const body = rows.map((r) => cols.map((c) => {
      const v = r[c];
      if (v == null) return '';
      if (typeof v === 'number') return String(Number(v.toFixed(6)));
      return `"${String(v).replace(/"/g, '""')}"`;
    }).join(','));
    // \uFEFF = BOM：没有它 Excel 会把中文列名按本地编码解出乱码
    const csv = [head, ...body].join('\n');
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}`], {
      type: 'text/csv;charset=utf-8',
    }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `fundamental-${data?.asof ?? 'rank'}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // 归一化分数分布：只知道排行榜前列没有意义，还要知道「这个分数段有多少票」
  const distribution = useMemo(() => {
    if (!rows.length) return null;
    const bins = new Array(10).fill(0);
    for (const r of rows) {
      const idx = Math.min(9, Math.max(0, Math.floor(r.normalized_score / 10)));
      bins[idx] += 1;
    }
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: {
        type: 'category',
        data: bins.map((_, i) => `${i * 10}-${i * 10 + 10}`),
        axisLine: { lineStyle: { color: '#d8d5cd' } },
        axisLabel: { fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: '#eeece6' } },
        axisLabel: { fontSize: 10 },
      },
      series: [{
        type: 'bar',
        data: bins,
        itemStyle: { color: '#4f5bd5' },
        barMaxWidth: 28,
      }],
    };
  }, [rows]);

  const sortIndicator = (col: string) => (sortBy === col ? (sortDesc ? ' ↓' : ' ↑') : '');

  return (
    <div className="space-y-5">
      <PageHeader
        title="基本面排名"
        sub="行业相对分位评分（PIT：只用观察日已公告的报表）"
        actions={
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={exportCsv}
                    disabled={!rows.length}>
              导出 CSV
            </button>
            <Link href="/factors" className="btn">去因子页</Link>
          </div>
        }
      />

      {/* 数据源可用性：报表明细 / 派生 / 日线估值是三个独立来源 */}
      {(fin || val) && (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 border border-line bg-panel px-4 py-2 text-xs">
          <span className="text-ink-faint">数据源</span>
          <span className={fin?.available ? 'text-up' : 'text-down'}>
            财务报表 {fin?.available
              ? `已就绪（${fin.rows.toLocaleString()} 行${fin.lookback_years ? `，近 ${fin.lookback_years} 年报告期` : ''}）`
              : '不可用'}
          </span>
          <span className={val?.available ? 'text-up' : 'text-down'}>
            日线估值 {val?.available
              ? `已就绪（${val.rows.toLocaleString()} 行）`
              : '不可用，估值模块 20 分恒为 0'}
          </span>
          {fin && !fin.available && fin.hint && (
            <span className="font-mono text-ink-faint">{fin.hint}</span>
          )}
          {val && !val.available && val.hint && (
            <span className="font-mono text-ink-faint">{val.hint}</span>
          )}
        </div>
      )}

      <div className="flex gap-1 border-b border-line">
        {([['rank', '排名'], ['percentile', '行业分位'], ['reconcile', '三表勾稽']] as const)
          .map(([k, label]) => (
            <button key={k} type="button" onClick={() => setTab(k)}
                    className={`-mb-px border-b-2 px-3 py-1.5 text-[13px] ${
                      tab === k
                        ? 'border-indigo font-semibold text-ink'
                        : 'border-transparent text-ink-dim hover:text-ink'
                    }`}>
              {label}
            </button>
          ))}
      </div>

      {tab === 'percentile' && <PercentileMatrix asof={asof} minSamples={minSamples} />}
      {tab === 'reconcile' && <ReconcilePanel defaultSymbol={debouncedSearch} />}

      {tab === 'rank' && (
        <>
          <Panel title="筛选">
            <div className="flex flex-wrap items-end gap-4">
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">观察日（留空=今天）</div>
                <input type="date" value={asof} onChange={(e) => setAsof(e.target.value)}
                       aria-label="观察日" className="input input-mono" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">代码搜索</div>
                <input value={search} onChange={(e) => setSearch(e.target.value)}
                       placeholder="600519" aria-label="代码搜索"
                       className="input input-mono" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">
                  最低覆盖率 {(minCoverage * 100).toFixed(0)}%
                </div>
                <input type="range" min={0} max={1} step={0.05} value={minCoverage}
                       aria-label="最低覆盖率"
                       onChange={(e) => setMinCoverage(+e.target.value)}
                       className="w-40" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint"
                     title="行业样本不足此数的指标不出分位">
                  最小行业样本 {minSamples}
                </div>
                <input type="number" min={2} max={100} value={minSamples}
                       aria-label="最小行业样本"
                       onChange={(e) => setMinSamples(
                         Math.min(100, Math.max(2, +e.target.value || 5)))}
                       className="input w-20" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">条数</div>
                <select value={limit} onChange={(e) => setLimit(+e.target.value)}
                        aria-label="条数" className="input">
                  {[20, 50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <button type="button" className="btn" onClick={reset}>重置</button>
            </div>

            {(industryList?.rows?.length ?? 0) > 0 && (
              <div className="mt-3">
                <div className="mb-1 flex items-center gap-2 text-xs text-ink-faint">
                  <span>行业（可多选）</span>
                  {industries.length > 0 && (
                    <button type="button" className="text-indigo hover:underline"
                            onClick={() => setIndustries([])}>
                      清空
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-1">
                  {industryList!.rows.map((r) => {
                    const on = industries.includes(r.industry);
                    return (
                      <button key={r.industry} type="button"
                              aria-pressed={on}
                              onClick={() => toggleIndustry(r.industry)}
                              className={`border px-2 py-0.5 text-xs ${
                                on ? 'border-indigo bg-indigo/10 text-indigo'
                                   : 'border-line text-ink-dim hover:text-ink'
                              }`}>
                        {r.industry}
                        <span className="ml-1 text-ink-faint">{r.n}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </Panel>

          <Panel
            title="评分结果"
            meta={[
              data?.asof ? `观察日 ${data.asof}` : '',
              data?.n_total != null ? `命中 ${data.n_scored}/${data.n_total}` : '',
            ].filter(Boolean).join(' · ')}
          >
            {isLoading && <Loading />}
            {error && <ErrorNote>加载失败：{String(error)}</ErrorNote>}
            {!isLoading && !error && data && !data.available && (
              <Empty>{data.hint ?? '暂无数据'}</Empty>
            )}
            {!isLoading && !error && data?.available && (
              rows.length === 0 ? (
                <Empty>
                  当前筛选条件下没有标的 —— 试试降低最低覆盖率
                  {industries.length > 0 ? '、清空行业筛选' : ''}
                </Empty>
              ) : (
                <>
                  {/* 模块可得性：把「模块无数据源」和「本票没匹配上」区分开 */}
                  {data.modules && modules.length > 0 && (
                    <div className="mb-3 flex flex-wrap gap-4 text-xs">
                      {modules.map((m) => {
                        const st = data.modules?.[m.name];
                        if (!st) return null;
                        const dead = st.n_scored_rows === 0;
                        return (
                          <span key={m.name}
                                className={dead ? 'text-down' : 'text-ink-dim'}>
                            {m.label}
                            <span className="ml-1 tabular-nums">
                              {(st.hit_rate * 100).toFixed(0)}%
                            </span>
                            {dead && <span className="ml-1">（无数据源）</span>}
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {distribution && (
                    <div className="mb-4 border border-line">
                      <div className="border-b border-line px-3 py-1 text-xs text-ink-faint">
                        归一化分数分布（当前筛选结果 {rows.length} 只）
                      </div>
                      <Chart option={distribution} height={160} />
                    </div>
                  )}

                  <div className="overflow-x-auto">
                    <table className="table-dense">
                      <thead>
                        <tr>
                          <th className="w-10 text-right">#</th>
                          <th className="text-left">
                            <button type="button" onClick={() => toggleSort('symbol')}>
                              标的{sortIndicator('symbol')}
                            </button>
                          </th>
                          <th className="text-left">行业</th>
                          <th className="text-right">
                            <button type="button" onClick={() => toggleSort('normalized_score')}>
                              归一化分{sortIndicator('normalized_score')}
                            </button>
                          </th>
                          <th className="text-right">
                            <button type="button" onClick={() => toggleSort('coverage')}>
                              覆盖率{sortIndicator('coverage')}
                            </button>
                          </th>
                          <th className="text-right">
                            <button type="button" onClick={() => toggleSort('n_scored')}>
                              命中{sortIndicator('n_scored')}
                            </button>
                          </th>
                          <th className="text-left">评级</th>
                          {modules.map((m) => (
                            <th key={m.name} className="text-right">{m.label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((r, i) => (
                          <tr key={r.symbol} className="hover:bg-white">
                            <td className="text-right tabular-nums text-ink-faint">{i + 1}</td>
                            <td className="font-mono">
                              <Link href={`/security/${r.symbol}`}
                                    className="text-indigo hover:underline">
                                {r.symbol}
                              </Link>
                            </td>
                            <td>{r.industry ?? '—'}</td>
                            <td className="text-right tabular-nums">
                              {r.normalized_score.toFixed(1)}
                            </td>
                            <td className="text-right tabular-nums text-ink-dim">
                              {(r.coverage * 100).toFixed(0)}%
                            </td>
                            <td className="text-right tabular-nums text-ink-faint">
                              {r.n_scored}/{r.n_metrics}
                            </td>
                            <td className={`${RATING_TONE[r.rating] ?? 'text-ink'}`}>
                              {r.rating}
                            </td>
                            {modules.map((m) => (
                              <td key={m.name} className="text-right tabular-nums">
                                {Number(r[`score_${m.name}`] ?? 0).toFixed(1)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )
            )}
          </Panel>

          <Panel title="评分口径" meta="模块权重合计 100">
            {!catalogue ? <Loading /> : (
              <>
                <div className="grid grid-cols-2 gap-y-3 md:grid-cols-5">
                  {modules.map((m) => (
                    <Stat key={m.name} label={m.label} value={
                      <span className="tabular-nums">{m.weight}</span>
                    } hint={m.n_metrics ? `${m.n_metrics} 项` : undefined} />
                  ))}
                </div>
                <div className="mt-4 overflow-x-auto">
                  <table className="table-dense">
                    <thead>
                      <tr>
                        <th className="text-left">指标</th>
                        <th className="text-left">模块</th>
                        <th className="text-left">取数来源</th>
                        <th className="text-right">满分</th>
                        <th className="text-left">方向</th>
                      </tr>
                    </thead>
                    <tbody>
                      {catalogue.metrics.map((m) => (
                        <tr key={m.item} className="hover:bg-white">
                          <td>
                            {m.label}
                            <span className="ml-1 font-mono text-xs text-ink-faint">
                              {m.item}
                            </span>
                          </td>
                          <td>{modules.find((x) => x.name === m.module)?.label ?? m.module}</td>
                          <td className="text-ink-dim">
                            {SOURCE_LABEL[m.source ?? 'pit'] ?? m.source}
                          </td>
                          <td className="text-right tabular-nums">{m.max_score}</td>
                          <td className="text-ink-dim">{m.direction}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-3 text-xs text-ink-faint">
                  每个指标按「同行业内的相对位置」给分：正向指标 ≥P75 满分，反向指标 ≤P25 满分。
                  分位只用观察日已公告的报表计算，样本不足的行业不出分位（该指标不计分，覆盖率会下降）。
                  「派生」指标由原始报表科目现算（如同一报告期的经营现金流 ÷ 归母净利润）；
                  「估值」取自日线湖的 PE/PB/股息率，PE、PB 为负（亏损或净资产为负）时不计分。
                </p>
              </>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
