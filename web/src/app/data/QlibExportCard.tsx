'use client';

/** Qlib 数据导出卡片：数据状态 + 导出表单（进度见任务中心）。 */
import { useCallback, useEffect, useState } from 'react';
import { Panel } from '@/components/Panel';
import {
  EXPORT_FIELDS,
  getQlibStatus,
  postQlibExport,
  type QlibStatus,
} from '@/lib/qlib';

const FIELD_LABELS: Record<string, string> = {
  open: '开盘', high: '最高', low: '最低', close: '收盘', volume: '成交量',
  amount: '成交额', vwap: 'VWAP', factor: '复权因子',
  turnover_rate: '换手率', total_mv: '总市值', float_mv: '流通市值',
  pe_ttm: 'PE', pb_mrq: 'PB', ps_ttm: 'PS', pct_chg: '涨跌幅',
};

export default function QlibExportCard() {
  const [status, setStatus] = useState<QlibStatus | null>(null);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [top, setTop] = useState('');
  const [fields, setFields] = useState<string[]>([]);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    getQlibStatus().then(setStatus).catch((e) => setMsg(String(e)));
  }, []);
  useEffect(refresh, [refresh]);

  async function submit() {
    setMsg('');
    setBusy(true);
    try {
      const body: ExportBody = {};
      if (start) body.start = start;
      if (end) body.end = end;
      if (top) body.top = Number(top);
      if (fields.length > 0) body.fields = [...fields];
      const r = await postQlibExport(body);
      setMsg(`✓ 已提交导出任务 ${r.job_id}，进度见任务中心`);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  const cal = status?.calendar;
  const symbols = status?.manifest
    ? String((status.manifest as Record<string, unknown>).symbols ?? '—')
    : null;

  return (
    <Panel title="Qlib 数据导出" meta="日线湖 → qlib 二进制">
      <div className="space-y-3 text-sm">
        {!status?.exists ? (
          <p className="text-ink-faint">尚未导出 qlib 数据（data/qlib 不存在）。</p>
        ) : (
          <div className="space-y-1">
            {cal && (
              <p>
                日历：{cal.start} ~ {cal.end}（{cal.days} 个交易日）
              </p>
            )}
            {symbols && <p>标的：{symbols}</p>}
          </div>
        )}
        <div className="grid grid-cols-3 gap-2">
          <label className="text-xs text-ink-faint">
            开始
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
              className="input input-mono w-full" />
          </label>
          <label className="text-xs text-ink-faint">
            结束
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)}
              className="input input-mono w-full" />
          </label>
          <label className="text-xs text-ink-faint">
            TopN
            <input type="number" value={top} onChange={(e) => setTop(e.target.value)}
              className="input input-mono w-full" />
          </label>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {EXPORT_FIELDS.map((f) => (
            <label key={f} className="flex items-center gap-1">
              <input type="checkbox" checked={fields.includes(f)}
                onChange={(e) =>
                  setFields((old) =>
                    e.target.checked ? [...old, f] : old.filter((x) => x !== f),
                  )
                } />
              {FIELD_LABELS[f] ?? f}
            </label>
        ))}
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? '提交中…' : '导出'}
          </button>
          <button className="btn" onClick={refresh}>刷新状态</button>
        </div>
        {msg && <p className="text-xs text-ink-faint">{msg}</p>}
      </div>
    </Panel>
  );
}

type ExportBody = Parameters<typeof postQlibExport>[0];
