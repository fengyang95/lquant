// 因子轮动快速回测表单 —— 逻辑原样迁自旧回测页「运行回测」Panel；自持 state + post('/backtests/run')，
// 唯一改动：成功后全局 mutate('/backtests') 刷新历史列表。
'use client';

import { useState } from 'react';
import { mutate } from 'swr';
import { Panel } from '@/components/Panel';
import { ErrorNote } from '@/components/States';
import { post } from '@/lib/api';

const FORMULAS = ['pct_change_5', 'pct_change_10', 'pct_change_20', 'rolling_std_20'];
const REBALANCES = ['daily', 'weekly', 'monthly'];

export default function QuickRunPanel() {
  const [formula, setFormula] = useState('pct_change_20');
  const [topN, setTopN] = useState(5);
  const [rebalance, setRebalance] = useState('monthly');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function run() {
    setBusy(true);
    setErr('');
    try {
      await post('/backtests/run', { top_n: topN, rebalance, formula });
      await mutate('/backtests');
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="运行回测">
      {err && <ErrorNote>{err}</ErrorNote>}
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <div className="mb-1 text-xs text-ink-faint">因子公式</div>
          <select value={formula} onChange={(e) => setFormula(e.target.value)} className="input">
            {FORMULAS.map((f) => <option key={f}>{f}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-ink-faint">TopN</div>
          <input type="number" min={1} max={50} value={topN}
                 onChange={(e) => {
                   // 清空/非法输入回退默认值，避免 NaN 提交
                   const v = +e.target.value;
                   setTopN(Number.isFinite(v) && v > 0 ? Math.min(50, Math.max(1, Math.round(v))) : 5);
                 }}
                 className="input input-mono w-20" />
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-ink-faint">调仓频率</div>
          <select value={rebalance} onChange={(e) => setRebalance(e.target.value)} className="input">
            {REBALANCES.map((r) => <option key={r}>{r}</option>)}
          </select>
        </label>
        <button onClick={run} disabled={busy} className="btn btn-accent">
          {busy ? '回测中…' : '运行回测'}
        </button>
      </div>
    </Panel>
  );
}
