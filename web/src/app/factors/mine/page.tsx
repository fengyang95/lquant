"use client";

/**
 * /factors/mine —— 挖掘会话台账 + Agent 管理面板（M4a 简版）。
 */
import useSWR from 'swr';
import { useState } from 'react';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty } from '@/components/States';
import { get, post } from '@/lib/api';

type MineRun = {
  run_id: string; agent: string; generator: string;
  n_evaluated: number; n_static_fail: number; n_low_ic: number;
  n_redundant: number; n_size_proxy: number; n_survivors: number;
  created_at: string;
};
type AgentRow = {
  name: string; kind: string; driver: string; enabled: boolean;
  quota_eval: number; can_submit: boolean;
};

function KIND_COLOR(kind: string): string {
  return kind === 'builtin' ? '#31589E' : kind === 'skill' ? '#1E7C55' : '#B08A3E';
}

function StatRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-ink-dim">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  );
}

export default function FactorsMinePage() {
  const { data: runs, mutate: mutateRuns } = useSWR<MineRun[]>('/factors/mine/runs', get);
  const { data: agents } = useSWR<AgentRow[]>('/factors/agents', get);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [n, setN] = useState(100);

  async function runMining(generator: string) {
    setBusy(generator);
    setMsg('');
    try {
      const r = await post<{ n_evaluated: number; n_survivors: number; run_id: string }>(
        '/factors/mine/run',
        { agent: 'gp-internal', generator, n: n });
      setMsg(`run ${r.run_id}: 评估 ${r.n_evaluated} / 幸存 ${r.n_survivors}`);
      mutateRuns();
    } catch (e) {
      setMsg(`失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader title="因子挖掘" sub="门禁 G0-G3 · random 基线 · GP/LLM 提案 · 同一门禁同一口径" />
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Agent 管理面板" meta="kind x driver · 配额与权限 · lq agent test 验收">
          {(agents?.length ?? 0) === 0 ? (
            <Empty>未注册 Agent（config/agents/*.yaml）</Empty>
          ) : (
            <div className="space-y-2 text-xs">
              {agents!.map((a) => (
                <div key={a.name} className="flex items-center gap-2 rounded-md border p-2">
                  <span className="rounded-sm px-1.5 py-0.5 font-mono text-[10px] text-white"
                    style={{ background: KIND_COLOR(a.kind) }}>
                    {a.kind}/{a.driver}
                  </span>
                  <span className="font-medium">{a.name}</span>
                  <span className="text-ink-faint">配额 {a.quota_eval}</span>
                  {a.can_submit && <span className="text-ink-dim">可 submit</span>}
                  {!a.enabled && <span className="text-ink-faint">已冻结</span>}
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="触发挖掘会话" meta="平台驱动：random 基线 / GP / LLM 提案（JSONL）">
          <div className="flex flex-wrap items-center gap-2">
            {['random', 'gp'].map((g) => (
              <button key={g} onClick={() => runMining(g)} disabled={busy !== ''}
                className="btn btn-sm">
                {busy === g ? '运行中…' : `跑 ${n} 个 ${g.toUpperCase()} 候选`}
              </button>
            ))}
            <input type="number" value={n} min={10} max={5000}
              onChange={(e) => setN(Number(e.target.value))}
              className="input w-24 py-1 text-xs" />
            <span className="text-xs text-ink-faint">预算（受 Agent 配额约束）</span>
          </div>
          {msg && <p className="mt-2 text-xs text-ink-dim">{msg}</p>}
        </Panel>
      </div>

      <Panel title="挖掘台账" meta="漏斗：评估 → G0 / LOW_IC / 冗余 / 风格代理 → 幸存">
        {runs && runs.length > 0 ? (
          <table className="w-full text-xs">
            <thead><tr>
              <th className="text-left">run_id</th><th className="text-left">Agent</th>
              <th>生成器</th><th>评估</th><th>G0淘汰</th><th>LOW_IC</th>
              <th>冗余</th><th>风格代理</th><th>幸存</th>
            </tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id}>
                  <td className="font-mono">{r.run_id}</td>
                  <td>{r.agent}</td>
                  <td className="text-center">{r.generator}</td>
                  <td className="text-center">{r.n_evaluated}</td>
                  <td className="text-center">{r.n_static_fail}</td>
                  <td className="text-center">{r.n_low_ic}</td>
                  <td className="text-center">{r.n_redundant}</td>
                  <td className="text-center">{r.n_size_proxy}</td>
                  <td className="font-mono">{r.n_survivors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有挖掘会话 —— 上方触发一次</Empty>
        )}
      </Panel>
    </div>
  );
}
