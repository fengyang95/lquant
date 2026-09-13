'use client';

import { useState } from 'react';
import { Panel, Stat } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import { post } from '@/lib/api';

type CheckIssue = {
  rule: string;
  severity: string;
  detail: string;
  dataset: string;
  symbol: string | null;
  trade_date: string | null;
  count: number;
};
type CheckResult = { summary: Record<string, number>; issues: CheckIssue[] };

/** 严重级别展示口径：fatal 红加粗 / error 红 / warn 金 / info 淡（红涨绿跌配色下的告警色） */
function sevClass(sev: string): string {
  if (sev === 'fatal') return 'text-up font-semibold';
  if (sev === 'error') return 'text-up';
  if (sev === 'warn') return 'text-gold';
  return 'text-ink-faint';
}
const sevText: Record<string, string> = {
  fatal: 'fatal',
  error: 'error',
  warn: 'warn',
  info: 'info',
};

/** 全湖质量检查卡：`lq data check` 的前端入口。
 *  触发序列/截面级 validators（涨跌停/覆盖度/僵尸/复权因子/日历对齐），
 *  issue 同步落 data_quality_issue，与 CLI / 同步链路共用一张表。 */
export default function QualityPanel() {
  const [result, setResult] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  async function runCheck() {
    setBusy(true);
    setMsg('');
    try {
      const r = await post<CheckResult>('/data/check', {});
      setResult(r);
      const s = r.summary;
      setMsg(
        s.total === 0
          ? '✓ 质量检查通过：全湖无 issue'
          : `✓ 检查完成：${s.total} 条 issue（fatal ${s.fatal ?? 0} / error ${s.error ?? 0} / warn ${s.warn ?? 0} / info ${s.info ?? 0}），已落 data_quality_issue`,
      );
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  const s = result?.summary;

  return (
    <Panel
      title="质量检查"
      meta="全湖校验 · fatal 阻断口径（此处仅展示，不阻断）"
      actions={
        <button className="btn btn-sm btn-primary" onClick={runCheck} disabled={busy}>
          {busy ? '检查中…' : '运行全湖检查'}
        </button>
      }
    >
      <Msg text={msg} />
      {s ? (
        <div className="mb-3 grid grid-cols-2 gap-y-3 divide-line sm:grid-cols-5 sm:divide-x">
          <div className="sm:pr-4">
            <Stat label="issue 总数" value={(s.total ?? 0).toLocaleString()} tone={s.total ? 'text-up' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="fatal" value={(s.fatal ?? 0).toLocaleString()} tone={s.fatal ? 'text-up font-semibold' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="error" value={(s.error ?? 0).toLocaleString()} tone={s.error ? 'text-up' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="warn" value={(s.warn ?? 0).toLocaleString()} tone={s.warn ? 'text-gold' : undefined} />
          </div>
          <div className="sm:px-4">
            <Stat label="info" value={(s.info ?? 0).toLocaleString()} />
          </div>
        </div>
      ) : (
        <Empty>
          尚未检查 —— 点右上角「运行全湖检查」，等价于 CLI <code className="bg-paper px-1">lq data check</code>
          （warn 属可接受范围，fatal/error 需要排查）
        </Empty>
      )}
      {result && result.issues.length > 0 && (
        <div className="max-h-80 overflow-auto">
          <table className="table-dense">
            <thead>
              <tr>
                <th className="w-16 text-left">级别</th>
                <th className="text-left">规则</th>
                <th className="w-24 text-left">标的</th>
                <th className="w-24 text-left">日期</th>
                <th className="w-16 text-right">行数</th>
                <th className="text-left">说明</th>
              </tr>
            </thead>
            <tbody>
              {result.issues.map((it, idx) => (
                <tr key={`${it.rule}-${it.symbol ?? ''}-${it.trade_date ?? ''}-${idx}`}>
                  <td className={`text-xs ${sevClass(it.severity)}`}>{sevText[it.severity] ?? it.severity}</td>
                  <td className="font-mono text-xs">{it.rule}</td>
                  <td className="font-mono text-xs">{it.symbol ?? '—'}</td>
                  <td className="text-xs">{it.trade_date ?? '—'}</td>
                  <td className="text-right font-mono text-xs">{it.count ? it.count.toLocaleString() : '—'}</td>
                  <td className="max-w-80 truncate text-xs text-ink-dim" title={it.detail}>{it.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {s && s.total > result.issues.length && (
            <div className="mt-2 text-xs text-ink-faint">
              仅展示前 {result.issues.length} 条，全部 {s.total} 条已落库（可在 CLI 查 data_quality_issue 表）
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
