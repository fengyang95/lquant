'use client';

/**
 * 个股分析入口页：默认展示**全量标的清单**（分页），可按行业 / 代码 / 名称 /
 * 类型 / 板块 + **股价 / 总市值 / 成交额**区间筛选与排序，点任意一行进入
 * `/security/{symbol}` 的多角度分析。
 *
 * 为什么要有全量清单：原来这一页只有「输入代码」搜索框 + 自选列表 —— 不记得
 * 代码的用户没有入口。清单默认加载，行业归属走后端 PIT 口径
 * （`industry_classify.std_date <= asof`），与行业分析 / 因子中性化一致。
 *
 * 价 / 额 / 市值来自后端湖内快照（日线湖 + 估值湖），**不依赖实时行情源**：
 * 实时接口不可用时这些列照样有值。估值湖的口径日单独透出（`mv_date`）——
 * 它天然滞后于日线，不标注就会让人以为市值是当天的。
 */
import { useEffect, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
import { Pct, fmtNum, fmtYi } from '@/components/QuoteTable';
import { fetcher, post } from '@/lib/api';

type UniverseItem = {
  symbol: string;
  name: string | null;
  sec_type: string | null;
  board: string | null;
  list_date: string | null;
  is_st: boolean | null;
  industry: string | null;
  industry_code: string | null;
  close: number | null;
  pct_chg: number | null;
  amount: number | null;
  total_mv: number | null;
  float_mv: number | null;
  turnover_rate: number | null;
  pe_ttm: number | null;
  /** 价 / 额的口径日（日线末根） */
  price_date: string | null;
  /** 总市值 / 换手率的口径日（估值湖，通常滞后于日线） */
  mv_date: string | null;
};

type IndustryFacet = { code: string | null; name: string | null; n_members: number };

type UniverseResp = {
  asof: string | null;
  std: string | null;
  total: number;
  limit: number;
  offset: number;
  items: UniverseItem[];
  industries: IndustryFacet[];
  notes: string[];
};

type WatchRow = {
  symbol: string;
  name: string | null;
  close: number | null;
  trade_date: string | null;
  change_pct: number | null;
};

type AngleRow = { id: string; label: string; weight: number; desc: string };

/** 区间筛选的输入框状态。字符串而非 number：空串表示「不限」，
 *  而 `Number('') === 0` 会把它变成「≥ 0」这种看似无害实则不同的条件。 */
type RangeInputs = {
  minPrice: string; maxPrice: string;
  minMv: string; maxMv: string;
  minAmount: string; maxAmount: string;
};

const EMPTY_RANGES: RangeInputs = {
  minPrice: '', maxPrice: '', minMv: '', maxMv: '', minAmount: '', maxAmount: '',
};

/** 排序下拉：`字段:方向`。后端白名单见 /data/securities/universe 的 sort/order。 */
const SORTS = [
  { value: 'symbol:asc', label: '代码（升序）' },
  { value: 'total_mv:desc', label: '总市值（大到小）' },
  { value: 'total_mv:asc', label: '总市值（小到大）' },
  { value: 'amount:desc', label: '成交额（大到小）' },
  { value: 'close:desc', label: '股价（高到低）' },
  { value: 'close:asc', label: '股价（低到高）' },
  { value: 'turnover_rate:desc', label: '换手率（高到低）' },
];

/** 输入串 → 查询参数。非有限数 / 负数一律当「不限」—— 手输 `-`、`1e`、`.` 时
 *  不该把坏值发给后端（后端 ge=0 会 422，整张表变错误页）。 */
function numParam(raw: string): number | null {
  const s = raw.trim();
  if (!s) return null;
  const v = Number(s);
  return Number.isFinite(v) && v >= 0 ? v : null;
}

/** 每页行数。后端单页上限 200，50 行足够翻页又不至于让浏览器渲染太多不可见行 */
const PAGE_SIZE = 50;

/** 裸 6 位或带交易所后缀都接受（后端会做统一归一） */
const LOOKS_LIKE_CODE = /^\d{6}(\.(SH|SZ|BJ))?$/i;

const SEC_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'stock', label: '股票' },
  { value: 'etf', label: 'ETF' },
  { value: 'lof', label: 'LOF' },
  { value: 'index', label: '指数' },
];

const BOARDS = [
  { value: '', label: '全部板块' },
  { value: 'main', label: '主板' },
  { value: 'gem', label: '创业板' },
  { value: 'star', label: '科创板' },
  { value: 'bse', label: '北交所' },
];

const BOARD_LABEL: Record<string, string> = {
  main: '主板', gem: '创业板', star: '科创板', bse: '北交所', unknown: '—',
};

const SEC_TYPE_LABEL: Record<string, string> = {
  stock: '股票', etf: 'ETF', lof: 'LOF', index: '指数',
};

/** 一组「下限 ~ 上限」输入。label 带单位，aria-label 用完整中文（测试与读屏都靠它） */
function RangeInputs({
  label, minLabel, maxLabel, minValue, maxValue, onMin, onMax,
}: {
  label: string;
  minLabel: string;
  maxLabel: string;
  minValue: string;
  maxValue: string;
  onMin: (v: string) => void;
  onMax: (v: string) => void;
}) {
  return (
    <div className="text-sm">
      <div className="mb-1 text-xs text-ink-faint">{label}</div>
      <div className="flex items-center gap-1">
        <input
          inputMode="decimal"
          value={minValue}
          onChange={(e) => onMin(e.target.value)}
          aria-label={minLabel}
          placeholder="下限"
          className="input w-20 text-right tabular-nums"
        />
        <span className="text-ink-faint">~</span>
        <input
          inputMode="decimal"
          value={maxValue}
          onChange={(e) => onMax(e.target.value)}
          aria-label={maxLabel}
          placeholder="上限"
          className="input w-20 text-right tabular-nums"
        />
      </div>
    </div>
  );
}

export default function SecurityIndexPage() {
  const router = useRouter();
  const [q, setQ] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [industry, setIndustry] = useState('');
  const [secType, setSecType] = useState('');
  const [board, setBoard] = useState('');
  const [ranges, setRanges] = useState<RangeInputs>(EMPTY_RANGES);
  const [debouncedRanges, setDebouncedRanges] = useState<RangeInputs>(EMPTY_RANGES);
  const [sortKey, setSortKey] = useState('symbol:asc');
  const [page, setPage] = useState(0);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncMsg, setSyncMsg] = useState('');

  // 输入防抖 300ms 再发请求（与自选页同口径）
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  // 区间输入同理防抖：`1234` 敲到一半（`1`）就打一次全市场筛选是纯浪费
  useEffect(() => {
    const t = setTimeout(() => setDebouncedRanges(ranges), 300);
    return () => clearTimeout(t);
  }, [ranges]);

  // 筛选一变就回第一页：否则筛完只剩 1 页却停在第 5 页会看到空表
  useEffect(() => {
    setPage(0);
  }, [debouncedQ, industry, secType, board, debouncedRanges, sortKey]);

  const [sort, order] = sortKey.split(':');
  const params = new URLSearchParams();
  if (debouncedQ.trim()) params.set('q', debouncedQ.trim());
  if (industry) params.set('industry', industry);
  if (secType) params.set('sec_type', secType);
  if (board) params.set('board', board);
  const rangeParams: [string, string][] = [
    ['min_price', debouncedRanges.minPrice], ['max_price', debouncedRanges.maxPrice],
    ['min_mv', debouncedRanges.minMv], ['max_mv', debouncedRanges.maxMv],
    ['min_amount', debouncedRanges.minAmount], ['max_amount', debouncedRanges.maxAmount],
  ];
  for (const [key, raw] of rangeParams) {
    const v = numParam(raw);
    if (v != null) params.set(key, String(v));
  }
  params.set('sort', sort);
  params.set('order', order);
  params.set('limit', String(PAGE_SIZE));
  params.set('offset', String(page * PAGE_SIZE));
  const universeKey = `/data/securities/universe?${params.toString()}`;

  const { data: universe, isLoading: uniLoading, error: uniError,
          mutate: mutateUniverse } = useSWR<UniverseResp>(universeKey, fetcher);
  const { data: watch, isLoading: watchLoading, error: watchError } =
    useSWR<WatchRow[]>('/watchlist', fetcher);
  const { data: angleMeta } = useSWR<{ angles: AngleRow[] }>('/security/angles', fetcher);

  const items = universe?.items ?? [];
  const total = universe?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = !!(debouncedQ.trim() || industry || secType || board
    || Object.values(debouncedRanges).some((v) => numParam(v) != null));

  // 口径日：本页所有行里最晚的价 / 市值日期。逐行日期已透出，这里给一句总览，
  // 免得用户以为总市值是当天的（估值湖滞后是常态）。
  const latestOf = (pick: (it: UniverseItem) => string | null) =>
    items.map(pick).filter((d): d is string => !!d).sort().at(-1) ?? null;
  const priceDate = latestOf((it) => it.price_date);
  const mvDate = latestOf((it) => it.mv_date);

  const go = (symbol: string) => {
    const s = symbol.trim();
    if (s) router.push(`/security/${encodeURIComponent(s)}`);
  };

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    // 只有「看起来是代码」才直达详情页；名称关键字已在实时筛选下方清单
    if (LOOKS_LIKE_CODE.test(q.trim())) go(q);
  };

  const reset = () => {
    setQ('');
    setDebouncedQ('');
    setIndustry('');
    setSecType('');
    setBoard('');
    setRanges(EMPTY_RANGES);
    setDebouncedRanges(EMPTY_RANGES);
    setSortKey('symbol:asc');
    setPage(0);
  };

  /** 触发后端 POST /data/reference/sync（202 后台跑）—— 标的主档为空时的自助入口。
   *  只走快路径（sync_details=false）：慢路径逐只补上市日要 20-40 分钟，不适合按钮。 */
  async function syncReference() {
    setSyncBusy(true);
    setSyncMsg('');
    try {
      await post('/data/reference/sync', { sync_details: false });
      setSyncMsg('✓ 已提交后台同步（日历 / 标的清单 / 退市名单）—— 约 1 分钟后点「刷新」');
    } catch (e) {
      setSyncMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setSyncBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="个股分析"
        sub="全量标的清单 · 可按行业 / 代码 / 名称 / 类型 / 板块 + 股价 / 总市值 / 成交额筛选与排序；点「分析」进入多角度分析"
      />

      <Panel
        title="筛选"
        meta={universe
          ? `${universe.asof ?? '—'} · 行业标准 ${universe.std ?? '—'}`
          : undefined}
      >
        <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">代码 / 名称</div>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="600519 / 贵州茅台"
              aria-label="代码或名称"
              className="input input-mono w-56"
            />
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">行业</div>
            <select
              value={industry}
              onChange={(e) => setIndustry(e.target.value)}
              aria-label="行业"
              className="input w-48"
            >
              <option value="">全部行业</option>
              {(universe?.industries ?? []).map((i) => (
                <option key={i.code ?? i.name ?? ''} value={i.code ?? i.name ?? ''}>
                  {i.name ?? i.code}（{i.n_members}）
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">类型</div>
            <select
              value={secType}
              onChange={(e) => setSecType(e.target.value)}
              aria-label="类型"
              className="input w-28"
            >
              {SEC_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">板块</div>
            <select
              value={board}
              onChange={(e) => setBoard(e.target.value)}
              aria-label="板块"
              className="input w-28"
            >
              {BOARDS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
            </select>
          </label>
          <label className="text-sm">
            <div className="mb-1 text-xs text-ink-faint">排序</div>
            <select
              value={sortKey}
              onChange={(e) => setSortKey(e.target.value)}
              aria-label="排序"
              className="input w-40"
            >
              {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </label>
          <button type="button" className="btn" onClick={reset}>重置</button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!LOOKS_LIKE_CODE.test(q.trim())}
          >
            分析该代码
          </button>
        </form>

        {/* 区间筛选：股价（元）/ 总市值（亿元）/ 成交额（亿元）。单位写在标签上 ——
            同一个输入框里「市值 50」是 50 元还是 50 亿，不写清就一定有人填错。 */}
        <div className="mt-3 flex flex-wrap items-end gap-4 border-t border-line pt-3">
          <RangeInputs
            label="股价（元）"
            minLabel="股价下限"
            maxLabel="股价上限"
            minValue={ranges.minPrice}
            maxValue={ranges.maxPrice}
            onMin={(v) => setRanges((r) => ({ ...r, minPrice: v }))}
            onMax={(v) => setRanges((r) => ({ ...r, maxPrice: v }))}
          />
          <RangeInputs
            label="总市值（亿元）"
            minLabel="总市值下限"
            maxLabel="总市值上限"
            minValue={ranges.minMv}
            maxValue={ranges.maxMv}
            onMin={(v) => setRanges((r) => ({ ...r, minMv: v }))}
            onMax={(v) => setRanges((r) => ({ ...r, maxMv: v }))}
          />
          <RangeInputs
            label="成交额（亿元）"
            minLabel="成交额下限"
            maxLabel="成交额上限"
            minValue={ranges.minAmount}
            maxValue={ranges.maxAmount}
            onMin={(v) => setRanges((r) => ({ ...r, minAmount: v }))}
            onMax={(v) => setRanges((r) => ({ ...r, maxAmount: v }))}
          />
          <span className="pb-2 text-xs text-ink-faint">
            留空 = 不限；只对有数据的标的生效（市值 / 成交额为空的会落选）
          </span>
        </div>

        {q.trim() && !LOOKS_LIKE_CODE.test(q.trim()) && (
          <p className="mt-2 text-xs text-ink-faint">
            代码形如 6 位数字（可带 .SH/.SZ/.BJ）；名称关键字会实时筛选下方清单。
          </p>
        )}
      </Panel>

      <Panel
        title="全量标的"
        meta={universe ? `共 ${total} 只 · 第 ${page + 1}/${pageCount} 页` : undefined}
        bodyClass=""
        actions={
          <>
            <button type="button" className="btn text-xs"
                    onClick={() => mutateUniverse()}>
              刷新
            </button>
            <button type="button" className="btn text-xs" disabled={syncBusy}
                    onClick={syncReference}>
              {syncBusy ? '提交中…' : '同步标的清单'}
            </button>
          </>
        }
      >
        {syncMsg ? <div className="px-4 pt-3"><Msg text={syncMsg} /></div> : null}
        {uniLoading ? (
          <Loading />
        ) : uniError ? (
          <ErrorNote>加载失败：{String(uniError)}</ErrorNote>
        ) : !items.length ? (
          <div className="p-4">
            <Empty>
              {filtered ? (
                '没有匹配的标的 —— 换个筛选条件，或点「重置」'
              ) : (
                <>
                  标的库为空 —— 点右上角「同步标的清单」从数据源拉取
                  （等价于 <code className="bg-paper px-1">lq data reference</code>）
                </>
              )}
            </Empty>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="table-dense w-full">
                <thead>
                  <tr>
                    <th className="pl-4 text-left">代码</th>
                    <th className="text-left">名称</th>
                    <th className="text-left">行业</th>
                    <th className="text-left">板块</th>
                    <th className="text-left">类型</th>
                    <th className="text-right">最新价</th>
                    <th className="text-right">涨跌幅</th>
                    <th className="text-right">成交额</th>
                    <th className="text-right">总市值</th>
                    <th className="text-left">上市日</th>
                    <th className="pr-4 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.symbol} className="hover:bg-white">
                      <td className="pl-4 font-mono text-xs">{it.symbol}</td>
                      <td>
                        <span className="font-medium">{it.name || '—'}</span>
                        {it.is_st && (
                          <span className="ml-1.5 border border-up px-1 text-[10px] text-up">
                            ST
                          </span>
                        )}
                      </td>
                      <td className="text-xs">{it.industry || '—'}</td>
                      <td className="text-xs">
                        {BOARD_LABEL[it.board ?? ''] ?? it.board ?? '—'}
                      </td>
                      <td className="text-xs">
                        {SEC_TYPE_LABEL[it.sec_type ?? ''] ?? it.sec_type ?? '—'}
                      </td>
                      {/* 价 / 额 / 市值来自湖内快照：实时行情不可用时也有值，
                          口径日挂在 title 上，鼠标一停就能看到是哪天的数 */}
                      <td className="text-right tabular-nums"
                          title={it.price_date ? `行情口径 ${it.price_date}` : undefined}>
                        {fmtNum(it.close)}
                      </td>
                      <td className="text-right">
                        {it.pct_chg == null
                          ? <span className="text-flat">—</span>
                          : <Pct value={it.pct_chg / 100} />}
                      </td>
                      <td className="text-right tabular-nums text-ink-dim"
                          title={it.price_date ? `行情口径 ${it.price_date}` : undefined}>
                        {fmtYi(it.amount)}
                      </td>
                      <td className="text-right tabular-nums"
                          title={it.mv_date ? `市值口径 ${it.mv_date}` : undefined}>
                        {fmtYi(it.total_mv)}
                      </td>
                      <td className="text-xs text-ink-faint">{it.list_date ?? '—'}</td>
                      <td className="pr-4 text-right">
                        <Link
                          href={`/security/${encodeURIComponent(it.symbol)}`}
                          className="btn text-xs"
                        >
                          分析
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-line px-4 py-2 text-xs text-ink-faint">
              <span>
                第 {page + 1} / {pageCount} 页 · 共 {total} 只
                {priceDate ? ` · 行情口径 ${priceDate}` : ''}
                {mvDate ? ` · 市值口径 ${mvDate}` : ''}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  className="btn text-xs"
                  disabled={page <= 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  上一页
                </button>
                <button
                  type="button"
                  className="btn text-xs"
                  disabled={page + 1 >= pageCount}
                  onClick={() => setPage((p) => p + 1)}
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
        {universe?.notes?.length ? (
          <ul className="space-y-1 border-t border-line px-4 py-2 text-xs text-ink-faint">
            {universe.notes.map((n, i) => <li key={i}>· {n}</li>)}
          </ul>
        ) : null}
      </Panel>

      <Panel title="分析覆盖的角度" meta="每个角度取不到数会明确标注，不会用默认值填充">
        {!angleMeta?.angles?.length ? (
          <Empty>角度注册表不可用（后端 /api/security/angles 未响应）</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
            {angleMeta.angles.map((a) => (
              <div key={a.id} className="border-b border-line py-2">
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-medium text-ink">{a.label}</span>
                  <span className="text-[11px] text-ink-faint">
                    {a.weight > 0 ? `权重 ${(a.weight * 100).toFixed(0)}%` : '不参与评分'}
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] text-ink-faint">{a.desc}</div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="从自选进入" meta={watch?.length ? `${watch.length} 只` : undefined}>
        {watchLoading ? (
          <Loading />
        ) : watchError ? (
          <ErrorNote>加载失败：{String(watchError)}</ErrorNote>
        ) : !watch?.length ? (
          <Empty>
            自选为空 —— 先去{' '}
            <Link href="/watchlist" className="text-indigo hover:underline">自选页</Link>{' '}
            添加标的
          </Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">标的</th>
                <th className="text-right">最新价</th>
                <th className="text-right">涨跌幅</th>
                <th className="text-right">数据日期</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {watch.map((w) => (
                <tr key={w.symbol} className="hover:bg-white">
                  <td>
                    <span className="font-medium">{w.name || '—'}</span>
                    <span className="ml-2 font-mono text-xs text-ink-faint">{w.symbol}</span>
                  </td>
                  <td className="text-right tabular-nums">{fmtNum(w.close)}</td>
                  <td className="text-right"><Pct value={w.change_pct} /></td>
                  <td className="text-right text-xs text-ink-faint">{w.trade_date ?? '—'}</td>
                  <td className="text-right">
                    <Link href={`/security/${w.symbol}`} className="btn text-xs">分析</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
