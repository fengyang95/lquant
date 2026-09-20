'use client';

/** Qlib 运行结果区块：运行列表 + 指标 + 勾选对比 + 取消。 */
import { useCallback, useEffect, useState } from 'react';
import { Panel } from '@/components/Panel';
import {
  cancelQlibRun,
  compareQlibRuns,
  listQlibRuns,
  type CompareRow,
  type QlibRun,
} from '@/lib/qlib';

const METRIC_TEXT: Record<string, string> = {
  IC: 'IC', ICIR: 'ICIR', 'Rank IC': 'Rank IC', 'Rank ICIR': 'Rank ICIR',
  excess_return_without_cost: '超额收益(无成本)',
  excess_return_with_cost: '超额收益(含成本)',
};

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '运行中', finished: '已完成',
  failed: '失败', canceled: '已取消',
};

const fmt = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(4));

export default function QlibRunsSection() {
  const [runs, setRuns] = useState<QlibRun[]>([]);
  const [sel, setSel] = useState<string[]>([]);
  const [cmp, setCmp] = useState<{
    runs: { id: string; config: string; exp_name: string; status: string }[];
    rows: CompareRow[];
  } | null>(null);

  const refresh = useCallback(() => {
    listQlibRuns().then(setRuns).catch(() => undefined);
  }, []);
  useEffect(refresh, [refresh]);

  const toggle = (id: string) =>
    setSel((old) =>
      old.includes(id)
        ? old.filter((x) => x !== id)
        : old.length < 2 ? [...old, id] : [old[1], id],
    );

  async function doCompare() {
    if (sel.length === 2) {
      const r = await compareQlibRuns(sel as [string, string]);
      setCmp(r);
    }
  }

  async function cancel(id: string) {
    await cancelQlibRun(id);
    refresh();
  }

  return (
    <Panel title="Qlib 运行" meta="IC / Rank IC / 超额收益 · 勾选两项对比">
      <div className="space-y-3 text-sm">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="text-left text-xs text-ink-faint">
                <th>对比</th><th>ID</th><th>配置</th><th>状态</th>
                <th>IC</th><th>ICIR</th><th>超额(含成本)</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-t border-line">
                  <td><input type="checkbox" checked={sel.includes(r.id)}
                    onChange={() => toggle(r.id)}
                    disabled={r.status !== 'finished'} /></td>
                  <td className="font-mono text-xs">{r.id}</td>
                  <td>{r.config}</td>
                  <td>{STATUS_TEXT[r.status] ?? r.status}</td>
                  <td className="font-mono text-xs">{fmt(r.metrics?.IC)}</td>
                  <td className="larger font-mono text-xs">{fmt(r.metrics?.ICIR)}</td>
                  <td className="font-mono text-xs">{fmt(r.metrics?.excess_return_with_cost)}</td>
                  <td>
                    {r.status === 'running' || r.status === 'queued'
                      ? <button className="btn" onClick={() => cancel(r.id)}>取消</button>
                      : null}
                    {r.error && <span title={r.error}>⚠</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {runs.length === 0 && <p className="text-ink-faint">暂无运行记录。</p>}
        <button className="btn" disabled={sel.length !== 2} onClick={doCompare}>对比</button>
        {cmp && (
          <table className="w-full">
            <thead>
              <tr className="text-left text-xs text-ink-faint">
                <th>指标</th>
                {cmp.runs.map((r) => <th key={r.id}>{r.id}</th>)}
              </tr>
            </thead>
            <tbody>
              {cmp.rows.map((row) => (
                <tr key={row.metric} className="border-t border-line">
                  <td>{METRIC_TEXT[row.metric] ?? row.metric}</td>
                  {cmp.runs.map((r) => (
                    <td key={r.id} className="font-mono text-xs">
                      {fmt(Number(row[r.id]))}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Panel>
  );
}
