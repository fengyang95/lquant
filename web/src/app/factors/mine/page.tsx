"use client";

/**
 * /factors/mine —— 挖掘会话台账 + Agent 管理面板。
 * 增强：Agent 下拉（替代写死）、幸存因子展示与一键注册、Agent 验收指引（guide）。
 */
import useSWR from 'swr';
import { useState } from 'react';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty, Msg } from '@/components/States';
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
type Survivor = { expr: string; ic_neutral: number; t_stat: number; origin: string };
type MineResult = Omit<MineRun, 'created_at'> & { survivors: Survivor[] };
type AgentGuide = {
  agent: string; kind: string; driver: string; quota_eval: number;
  can_submit: boolean; steps: string[]; acceptance: string;
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
  const [agent, setAgent] = useState('gp-internal');
  const [survivors, setSurvivors] = useState<Survivor[]>([]);
  const [currentRun, setCurrentRun] = useState('');
  const [guide, setGuide] = useState<AgentGuide | null>(null);

  async function runMining(generator: string) {
    setBusy(generator);
    setMsg('');
    try {
      const r = await post<MineResult>('/factors/mine/run', { agent, generator, n });
      setMsg(`✓ run ${r.run_id}: 评估 ${r.n_evaluated} / 幸存 ${r.n_survivors}`);
      setSurvivors(r.survivors ?? []);
      setCurrentRun(r.run_id);
      mutateRuns();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function registerSurvivor(expr: string, i: number) {
    setBusy(`reg-${i}`);
    setMsg('');
    const name = `mine_${currentRun}_${i}`;
    try {
      await post('/factors', { name, expression: expr, description: `挖掘幸存 @${currentRun} #${i}` });
      setMsg(`✓ 已注册 ${name} → /factors/${name}`);
      mutateRuns();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function loadGuide(name: string) {
    setBusy(`guide-${name}`);
    setMsg('');
    try {
      setGuide(await get<AgentGuide>(`/factors/agents/${name}/guide`));
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader title="因子挖掘" sub="门禁 G0-G3 · random 基线 · GP/LLM 提案 · 同一门禁同一口径" />
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Agent 管理面板" meta="kind x driver · 配额与权限 · 点击查看验收指引">
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
                  <button className="font-medium hover:underline" onClick={() => loadGuide(a.name)}
                    title="查看验收指引">
                    {a.name}
                    {busy === `guide-${a.name}` ? ' …' : ''}
                  </button>
                </div>
              ))}
            </div>
          )}
          {guide && (
            <div className="mt-3 border border-line bg-paper p-3 text-xs">
              <div className="mb-1 font-semibold">{guide.agent} · 验收指引</div>
              <ol className="mb-2 list-decimal space-y-0.5 pl-4">
                {guide.steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
              <div className="text-ink-dim">验收标准：{guide.acceptance}</div>
              <button className="mt-2 text-indigo hover:underline" onClick={() => setGuide(null)}>收起</button>
              <span className="ml-3 text-ink-faint">
                配额 {guide.quota_eval} · {guide.can_submit ? '可 submit' : '不可 submit'}
              </span>
            </div>
          )}
        </Panel>
        <Panel title="触发挖掘会话" meta="平台驱动：random 基线 / GP（受 Agent 配额约束）">
          <div className="flex flex-wrap items-center gap-2">
            <select value={agent} onChange={(e) => setAgent(e.target.value)} className="input w-40 py-1 text-xs">
              {(agents ?? []).filter((a) => a.enabled).map((a) => (
                <option key={a.name} value={a.name}>{a.name}（配额 {a.quota_eval}）</option>
              ))}
            </select>
            {['random', 'gp'].map((g) => (
              <button key={g} onClick={() => runMining(g)} disabled={busy !== '' || !agent}
                className="btn btn-sm">
                {busy === g ? '运行中…' : `跑 ${n} 个 ${g.toUpperCase()} 候选`}
              </button>
            ))}
            <input type="number" value={n} min={10} max={5000}
              onChange={(e) => setN(Number(e.target.value))}
              className="input w-24 py-1 text-xs" />
            <span className="text-xs text-ink-faint">预算（受 Agent 配额约束）</span>
          </div>
          <Msg text={msg} />
        </Panel>
      </div>

      {survivors.length > 0 && (
        <>
          <Panel title={`幸存因子 · run ${currentRun}`} meta="G0-G3 门禁通过 · 可一键注册入库">
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">表达式</th>
                <th className="text-left">IC(中性化)</th>
                <th className="text-left">t 值</th>
                <th className="text-left">来源</th>
                <th className="text-left">操作</th>
              </tr>
            </thead>
            <tbody>
              {survivors.map((s, i) => (
                <tr key={i}>
                  <td className="font-mono text-xs">{s.expr}</td>
                  <td className="font-mono">{s.ic_neutral.toFixed(4)}</td>
                  <td className="font-mono">{s.t_stat.toFixed(2)}</td>
                  <td>{s.origin}</td>
                  <td>
                    <button className="btn btn-sm" disabled={busy !== ''}
                      onClick={() => registerSurvivor(s.expr, i)}>
                      {busy === `reg-${i}` ? '注册中…' : '注册'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </Panel>
          <p className="text-xs text-ink-faint">
            幸存表达式可直接注册，也可先在因子页做完整评价再决定。
          </p>
        </>
      )}

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
