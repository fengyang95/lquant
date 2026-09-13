// 基准验证面板 —— 引擎自检报告（GET /backtests/validation）+ 基准策略一键回测。
// 自检项与 tests/unit/test_backtest_accuracy.py 同源（lquant.backtest.selfcheck），
// 任何一项失败都意味着引擎语义出了问题，应当停止信任回测结果。
'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel, Stat } from '@/components/Panel';
import { ErrorNote, Loading } from '@/components/States';
import { get, post } from '@/lib/api';

type Check = { name: string; passed: boolean; detail: string };
type BenchmarkMeta = {
  label: string;
  symbols: string[];
  description: string;
  reference: string;
};
type Validation = {
  checks: Check[];
  all_passed: boolean;
  benchmarks: Record<string, BenchmarkMeta>;
};

export default function ValidationPanel() {
  const { data, isLoading, error, mutate } = useSWR<Validation>(
    '/backtests/validation', get,
  );
  const [busy, setBusy] = useState('');
  const [lastRun, setLastRun] = useState<{ run_id: string; label: string } | null>(null);
  const [err, setErr] = useState('');

  async function runBenchmark(key: string, label: string) {
    setBusy(key);
    setErr('');
    try {
      const r = await post<{ run_id: string }>('/backtests/run-benchmark', { key });
      setLastRun({ run_id: r.run_id, label });
      window.open(`/backtests/${r.run_id}`, '_blank');
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }

  if (isLoading) return <Loading>自检运行中…（含回测金标准逐项笔算）</Loading>;
  if (error) return <ErrorNote>加载失败：{String(error)}</ErrorNote>;
  if (!data) return null;

  const nPass = data.checks.filter((c) => c.passed).length;

  return (
    <div className="space-y-5">
      <Panel title="引擎自检" bodyClass="p-0"
        actions={
          <button className="btn btn-sm" onClick={() => void mutate()}>
            重新自检
          </button>
        }
      >
        <div className="grid grid-cols-2 divide-line sm:grid-cols-4 sm:divide-x">
          <div className="border-b border-line px-4 py-3 sm:border-b-0">
            <Stat label="自检项" value={String(data.checks.length)} />
          </div>
          <div className="border-b border-line px-4 py-3 sm:border-b-0">
            <Stat label="通过" value={`${nPass} / ${data.checks.length}`}
              tone={data.all_passed ? 'text-up' : 'text-down'} />
          </div>
          <div className="border-b border-line px-4 py-3 sm:border-b-0">
            <Stat label="整体状态" value={data.all_passed ? '全部通过' : '存在失败'}
              tone={data.all_passed ? 'text-up' : 'text-down'} />
          </div>
        </div>
        <div className="max-h-[420px] overflow-auto border-t border-line">
          <table className="table-dense text-xs">
            <thead>
              <tr className="sticky top-0 bg-panel">
                <th className="text-left">检查项</th>
                <th className="text-left">结果</th>
                <th className="text-left">详情</th>
              </tr>
            </thead>
            <tbody>
              {data.checks.map((c) => (
                <tr key={c.name} className="hover:bg-white">
                  <td>{c.name}</td>
                  <td className={c.passed ? 'text-up' : 'text-down'}>
                    {c.passed ? '✓ 通过' : '✗ 失败'}
                  </td>
                  <td className="text-ink-dim">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="基准策略 · 公开出处一键回测"
        meta="与 backtrader 独立引擎逐日对账的方法论见 docs/BACKTEST_VALIDATION_BENCHMARKS.md"
      >
        {err && <ErrorNote>{err}</ErrorNote>}
        {lastRun && (
          <div className="mb-3 rounded-[2px] border border-line bg-panel px-3 py-2 text-xs">
            ✓ {lastRun.label} 已运行：
            <a className="ml-1 text-indigo underline" href={`/backtests/${lastRun.run_id}`}>
              查看结果 {lastRun.run_id}
            </a>
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {Object.entries(data.benchmarks).map(([key, b]) => (
            <div key={key} className="rounded-[2px] border border-line bg-panel p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-medium">{b.label}</div>
                <button className="btn btn-sm btn-accent shrink-0"
                  disabled={busy === key}
                  onClick={() => void runBenchmark(key, b.label)}>
                  {busy === key ? '回测中…' : '运行回测'}
                </button>
              </div>
              <div className="mt-1 text-xs text-ink-dim">{b.description}</div>
              <div className="mt-1 text-xs text-ink-faint">
                标的：{b.symbols.join(' / ')}
              </div>
              <div className="mt-2 border-t border-line pt-2 text-xs text-ink-faint">
                <span className="font-medium text-gold">公开出处：</span>{b.reference}
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
