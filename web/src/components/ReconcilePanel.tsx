// 三表勾稽 —— 数据源 POST /fundamental/reconcile。
//
// 这个端点此前同样在后端存在但前端零引用，而且它要求调用方把七个报表科目
// 手工查出来填进去 —— 等于不可用。后端现在支持 auto 模式（缺项自动取
// 最近两期报表），这个面板默认走 auto，同时保留手填覆盖能力。
//
// **口径局限必须写在界面上**：留存收益勾稽式 |NI+OCI−ΔRE|/|NI| 没有扣除
// 利润分配与其它直接计入权益的变动，所以分红多的公司这一项会天然报出大差额。
// 不写清楚，用户会把「公司分红」误判成「数据错误」。
'use client';

import { useState } from 'react';
import { Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { post } from '@/lib/api';

type ReconcileItem = {
  name: string;
  gap: number | null;
  passed: boolean | null;
  score: number;
  note?: string;
};

type ReconcileResp = {
  symbol: string;
  passed: boolean;
  score: number;
  full_score?: number;
  checked_items?: string[];
  items: ReconcileItem[];
  inputs?: Record<string, number>;
  inputs_meta?: {
    asof: string;
    auto: boolean;
    latest_stat_date?: string;
    previous_stat_date?: string | null;
    period_basis?: string;
    from_request?: string[];
    from_financial_pit?: string[];
    auto_error?: string;
  };
};

const NAME_LABEL: Record<string, string> = {
  retained_earnings: '留存收益勾稽',
  cash_change: '现金变动勾稽',
  earnings_quality: '利润质量',
  ocf_to_ni: '经营现金流/净利润',
};

/** 手填覆盖用的字段（对应后端 ReconcileIn 的数值项） */
const OVERRIDE_FIELDS: { key: keyof Overrides; label: string }[] = [
  { key: 'net_income', label: '净利润' },
  { key: 'other_comprehensive', label: '其他综合收益' },
  { key: 'delta_retained', label: '留存收益变动' },
  { key: 'operating_cashflow', label: '经营现金流净额' },
  { key: 'cashflow_net_change', label: '现金净增加额' },
  { key: 'balance_cash_change', label: '货币资金变动' },
  { key: 'deducted_net_income', label: '扣非净利润' },
];

type Overrides = {
  net_income?: string;
  other_comprehensive?: string;
  delta_retained?: string;
  operating_cashflow?: string;
  cashflow_net_change?: string;
  balance_cash_change?: string;
  deducted_net_income?: string;
};

export default function ReconcilePanel({ defaultSymbol = '' }: { defaultSymbol?: string }) {
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [asof, setAsof] = useState('');
  const [showOverride, setShowOverride] = useState(false);
  const [overrides, setOverrides] = useState<Overrides>({});
  const [data, setData] = useState<ReconcileResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    const sym = symbol.trim();
    if (!sym) {
      setErr('请填写股票代码');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { symbol: sym, auto: true };
      if (asof) body.asof = asof;
      for (const [k, v] of Object.entries(overrides)) {
        // 空串表示「不覆盖」，交给后端自动取数
        if (v !== undefined && v !== '' && Number.isFinite(Number(v))) {
          body[k] = Number(v);
        }
      }
      setData(await post<ReconcileResp>('/fundamental/reconcile', body));
    } catch (e) {
      setData(null);
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const meta = data?.inputs_meta;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <div className="mb-1 text-xs text-ink-faint">股票代码</div>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)}
                 placeholder="600519.SH" aria-label="勾稽股票代码"
                 className="input input-mono" />
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-ink-faint">观察日（留空=今天）</div>
          <input type="date" value={asof} onChange={(e) => setAsof(e.target.value)}
                 aria-label="勾稽观察日" className="input input-mono" />
        </label>
        <button type="button" className="btn" onClick={run} disabled={busy}>
          {busy ? '校验中…' : '开始勾稽'}
        </button>
        <button type="button" className="btn"
                onClick={() => setShowOverride((v) => !v)}>
          {showOverride ? '收起手工覆盖' : '手工覆盖取值'}
        </button>
      </div>

      {showOverride && (
        <div className="grid grid-cols-2 gap-3 border border-line p-3 md:grid-cols-4">
          {OVERRIDE_FIELDS.map((f) => (
            <label key={f.key} className="text-sm">
              <div className="mb-1 text-xs text-ink-faint">{f.label}</div>
              <input className="input input-mono" inputMode="decimal"
                     aria-label={f.label}
                     value={overrides[f.key] ?? ''}
                     onChange={(e) => setOverrides((o) => ({ ...o, [f.key]: e.target.value }))} />
            </label>
          ))}
          <p className="col-span-full text-xs text-ink-faint">
            留空 = 由后端从最近两期报表自动取数（累计口径科目会先折算成单季）。
          </p>
        </div>
      )}

      {err && <ErrorNote>{err}</ErrorNote>}
      {busy && <Loading />}

      {!busy && data && (
        <>
          <div className="flex flex-wrap items-end gap-6">
            <Stat label="勾稽结果" value={data.passed ? '通过' : '未通过'}
                  tone={data.passed ? 'text-up' : 'text-down'} />
            <Stat label="得分" value={`${data.score}${data.full_score != null ? ` / ${data.full_score}` : ''}`} />
            {meta?.latest_stat_date && (
              <Stat label="报告期"
                    value={`${meta.latest_stat_date}${meta.previous_stat_date ? ` ← ${meta.previous_stat_date}` : ''}`}
                    hint={meta.period_basis === 'quarterly' ? '单季口径' : undefined} />
            )}
          </div>

          {meta?.auto_error && <p className="text-xs text-gold">自动取数：{meta.auto_error}</p>}
          {meta?.from_financial_pit?.length ? (
            <p className="text-xs text-ink-faint">
              自动取数项：{meta.from_financial_pit.join('、')}
              {meta.from_request?.length ? ` · 手工指定项：${meta.from_request.join('、')}` : ''}
            </p>
          ) : null}

          {data.items.length === 0 ? (
            <Empty>一项都没查到 —— 不算通过（数据缺失不能被当成质量优秀）</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="text-left">勾稽项</th>
                    <th className="text-right">相对差额</th>
                    <th className="text-left">判定</th>
                    <th className="text-right">得分</th>
                    <th className="text-left">口径</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((it) => (
                    <tr key={it.name} className="hover:bg-white">
                      <td>{NAME_LABEL[it.name] ?? it.name}</td>
                      <td className="text-right tabular-nums">
                        {it.gap == null ? '—' : `${(it.gap * 100).toFixed(1)}%`}
                      </td>
                      <td className={it.passed == null ? 'text-ink-faint'
                        : it.passed ? 'text-up' : 'text-down'}>
                        {it.passed == null ? '未检查' : it.passed ? '通过' : '未通过'}
                      </td>
                      <td className="text-right tabular-nums">{it.score}</td>
                      <td className="font-mono text-xs text-ink-faint">{it.note ?? ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-xs text-ink-faint">
            <span className="text-gold">口径局限：</span>
            留存收益勾稽式 |NI+OCI−ΔRE|/|NI| 未扣除利润分配、盈余公积转增等
            直接计入权益的变动，因此<strong>分红较多的公司这一项会天然报出较大差额</strong>，
            不代表数据错误。现金变动勾稽比较的是现金流量表的现金净变动与
            资产负债表货币资金变动，两者口径差异（如受限资金）也会形成残差。
          </p>
        </>
      )}

      {!busy && !data && !err && (
        <Empty>填入代码后点「开始勾稽」—— 默认从最近两期报表自动取数</Empty>
      )}
    </div>
  );
}
