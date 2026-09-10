'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel, Stat } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import { fetcher, post } from '@/lib/api';
import type { CrosscheckResult, QualityIssue } from './types';

/** 跨源印证卡：触发对拍 + 最近一次 summary + 字段级分歧明细（issues）+ 逐条 resolve。
 *  CROSS_SRC_DIFF 类 issue 的 detail 携带字段级明细（field/primary/peer/deviation_pct），
 *  由后端 Issue.extra 落库；缺失（如旧数据或非对拍规则）时对应列显示 —。 */
export default function CrosscheckPanel() {
  const { data: issues, mutate: mutateIssues } = useSWR<QualityIssue[]>(
    '/data/crosscheck/issues?limit=200',
    fetcher,
  );
  const [summary, setSummary] = useState<Record<string, number> | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [resolving, setResolving] = useState<string | null>(null);

  async function runCrosscheck() {
    setBusy(true);
    setMsg('');
    try {
      const res = await post<CrosscheckResult>('/data/crosscheck', {});
      setSummary(res.summary);
      setMsg(`✓ 对拍完成：${res.flagged_rows} 行被标记降级`);
      void mutateIssues();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function resolve(issueId: string) {
    setResolving(issueId);
    setMsg('');
    try {
      await post('/data/crosscheck/issues/resolve', { issue_id: issueId });
      void mutateIssues();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setResolving(null);
    }
  }

  return (
    <Panel
      title="跨源印证"
      meta="主源 vs peer 源抽检 · 分歧按 L1/L2/L3 分级"
      actions={
        <button className="btn btn-sm btn-primary" onClick={runCrosscheck} disabled={busy}>
          {busy ? '对拍中…' : '触发对拍'}
        </button>
      }
    >
      <Msg text={msg} />
      {summary && (
        <div className="mb-3 grid grid-cols-2 gap-y-3 divide-line sm:grid-cols-4 sm:divide-x">
          <div className="sm:pr-4">
            <Stat label="抽查行数" value={(summary.checked ?? 0).toLocaleString()} />
          </div>
          <div className="sm:px-4">
            <Stat label="L1 轻微" value={(summary.L1 ?? 0).toLocaleString()} />
          </div>
          <div className="sm:px-4">
            <Stat label="L2 可疑" value={(summary.L2 ?? 0).toLocaleString()} tone={summary.L2 ? 'text-up' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="L3 严重" value={(summary.L3 ?? 0).toLocaleString()} tone={summary.L3 ? 'text-up' : undefined} />
          </div>
        </div>
      )}

      {!issues?.length ? (
        summary && !summary.checked ? (
          <Empty>本次对拍 L0（未抽查任何行）—— 未配置可用的对拍 peer 源，请先在「数据源配置」勾选 peers</Empty>
        ) : (
          <Empty>暂无分歧 issue —— 触发对拍后这里展示 L2/L3 分歧明细</Empty>
        )
      ) : (
        <div className="max-h-80 overflow-auto">
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">标的</th>
                <th className="text-left">日期</th>
                <th className="text-left">字段</th>
                <th className="text-right">主源</th>
                <th className="text-right">peer</th>
                <th className="text-right">偏差%</th>
                <th className="text-left">级别</th>
                <th className="text-left">说明</th>
                <th className="w-16 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {issues.map((it) => {
                const d: QualityIssue['detail'] = it.detail ?? {};
                const dev = typeof d.deviation_pct === 'number'
                  ? `${d.deviation_pct.toFixed(2)}%`
                  : '—';
                const fmtVal = (v: unknown) =>
                  v == null ? '—' : String(v);
                return (
                  <tr key={it.issue_id}>
                    <td className="font-mono text-xs">{it.symbol ?? '—'}</td>
                    <td className="text-xs">{it.trade_date ?? '—'}</td>
                    <td className="font-mono text-xs">{d.field ?? '—'}</td>
                    <td className="text-right font-mono text-xs">{fmtVal(d.primary)}</td>
                    <td className="text-right font-mono text-xs">{fmtVal(d.peer)}</td>
                    <td className={`text-right font-mono text-xs ${dev === '—' ? 'text-ink-faint' : 'text-gold'}`}>{dev}</td>
                    <td className={`text-xs ${it.severity === 'error' ? 'text-up' : 'text-gold'}`}>
                      {it.severity === 'error' ? 'L3 严重' : 'L2 可疑'}
                    </td>
                    <td className="max-w-40 truncate text-xs text-ink-dim" title={d.message}>
                      {it.rule_code}
                      {d.message ? ` · ${d.message}` : ''}
                    </td>
                    <td className="text-right">
                      <button
                        className="btn btn-sm"
                        onClick={() => resolve(it.issue_id)}
                        disabled={resolving === it.issue_id}
                      >
                        {resolving === it.issue_id ? '…' : '忽略'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
