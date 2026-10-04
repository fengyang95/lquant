'use client';

/** 模型页：模型版本（晋级/回滚）、训练记录、信号覆盖。
 *
 * 三块对应 Phase 2 的三个问题：
 * - 版本表：现在线上是哪一版、能不能回滚；
 * - 训练记录：每次训练的指标与 artifact 在哪（可回放）；
 * - 信号覆盖：模型真的在出信号吗、出到哪一天。
 */
import { useState } from 'react';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading, Msg } from '@/components/States';
import { mlApi, rankIcOf, STAGE_BADGE, STAGE_TEXT } from '@/lib/ml';
import type { MlModelVersion, MlStage } from '@/lib/ml';

const STAGES: MlStage[] = ['production', 'staging', 'candidate', 'archived'];

function num(v: number | null | undefined, digits = 4): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—';
}

function ModelTable({
  models,
  onPromote,
  onRollback,
  busy,
}: {
  models: MlModelVersion[];
  onPromote: (m: MlModelVersion) => void;
  onRollback: (name: string) => void;
  busy: string;
}) {
  if (!models.length) {
    return <Empty>还没有任何模型版本。跑 `lq ml train …` 或在任务页发起训练。</Empty>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-ink-faint">
            <th className="py-2 pr-3">模型线</th>
            <th className="py-2 pr-3">版本</th>
            <th className="py-2 pr-3">阶段</th>
            <th className="py-2 pr-3 text-right">RankIC</th>
            <th className="py-2 pr-3">训练窗口</th>
            <th className="py-2 pr-3">artifact</th>
            <th className="py-2">操作</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={`${m.name}-${m.version}`} className="border-b border-line/60">
              <td className="py-2 pr-3">{m.name}</td>
              <td className="py-2 pr-3 tabular-nums">v{m.version}</td>
              <td className="py-2 pr-3">
                <span className={`border px-1.5 py-0.5 text-xs ${STAGE_BADGE[m.stage]}`}>
                  {STAGE_TEXT[m.stage]}
                </span>
              </td>
              <td className="py-2 pr-3 text-right tabular-nums">{num(rankIcOf(m.metrics))}</td>
              <td className="py-2 pr-3 text-xs text-ink-dim">
                {String(m.fit_window?.train_end ?? '—')} → {String(m.fit_window?.test_end ?? '—')}
              </td>
              <td className="max-w-[220px] truncate py-2 pr-3 text-xs text-ink-faint"
                  title={m.artifact_path ?? ''}>
                {m.artifact_path ?? '—'}
              </td>
              <td className="py-2">
                {m.stage !== 'production' && (
                  <button className="btn text-xs" disabled={!!busy}
                    onClick={() => onPromote(m)}>上线</button>
                )}
                {m.stage === 'production' && (
                  <button className="btn text-xs" disabled={!!busy}
                    onClick={() => onRollback(m.name)}>回滚</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MlPage() {
  const [stage, setStage] = useState<MlStage | ''>('');
  const { data: status } = useSWR('/ml/status', mlApi.status, { refreshInterval: 15_000 });
  const { data: models, mutate: mutateModels, error: modelsErr } = useSWR(
    ['/ml/models', stage],
    () => mlApi.models(undefined, stage || undefined),
    { refreshInterval: 20_000 },
  );
  const { data: runs } = useSWR('/ml/runs', () => mlApi.runs(30), { refreshInterval: 20_000 });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');

  async function promote(m: MlModelVersion) {
    setBusy('promote');
    setMsg('');
    try {
      await mlApi.promote(m.name, m.version, 'production', 'ui: promote');
      setMsg(`✓ ${m.name} v${m.version} 已上线`);
      void mutateModels();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function rollback(name: string) {
    setBusy('rollback');
    setMsg('');
    try {
      const mv = await mlApi.rollback(name);
      setMsg(`✓ ${name} 已回滚到 v${mv.version}`);
      void mutateModels();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  const signals = status?.signals ?? [];

  return (
    <div className="space-y-5">
      <PageHeader title="模型" sub="训练记录 · 版本晋级与回滚 · 每日信号（Phase 2 生产链路）" />

      <div className="grid grid-cols-2 gap-4 border border-line bg-panel p-4 lg:grid-cols-4">
        <Stat label="可用后端" value={(status?.backends ?? []).join(' / ') || '—'}
              hint={status ? `${status.model_lines} 条模型线` : ''} />
        <Stat label="版本总数" value={status?.versions ?? '—'}
              hint={status
                ? `线上 ${status.by_stage.production} · 候选 ${status.by_stage.candidate}`
                : ''} />
        <Stat label="线上模型" value={Object.keys(status?.production ?? {}).length || '—'}
              hint={Object.entries(status?.production ?? {})
                .map(([n, p]) => `${n} v${p.version}`).join(' · ')} />
        <Stat label="信号覆盖"
              value={signals.reduce((a, s) => a + s.days, 0) || '—'}
              hint={signals.map((s) => `${s.name}→${s.last}`).join(' · ')} />
      </div>

      <Msg text={msg} />
      {modelsErr && <ErrorNote>模型列表加载失败：{String(modelsErr)}</ErrorNote>}

      <Panel
        title="模型版本"
        meta={`${models?.length ?? 0} 个版本`}
        actions={
          <select className="input w-28 py-1 text-xs" value={stage}
                  onChange={(e) => setStage(e.target.value as MlStage | '')}>
            <option value="">全部阶段</option>
            {STAGES.map((s) => <option key={s} value={s}>{STAGE_TEXT[s]}</option>)}
          </select>
        }
      >
        {!models ? <Loading /> : (
          <ModelTable models={models} onPromote={promote} onRollback={rollback} busy={busy} />
        )}
      </Panel>

      <Panel title="训练记录" meta="ml_run · 每条可定位到 artifact（可回放）">
        {!runs ? <Loading /> : !runs.length ? (
          <Empty>还没有训练记录。</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-ink-faint">
                  <th className="py-2 pr-3">run_id</th>
                  <th className="py-2 pr-3">模型线</th>
                  <th className="py-2 pr-3">版本</th>
                  <th className="py-2 pr-3 text-right">RankIC</th>
                  <th className="py-2 pr-3 text-right">训练/测试行</th>
                  <th className="py-2 pr-3">区间</th>
                  <th className="py-2">创建</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id} className="border-b border-line/60">
                    <td className="py-2 pr-3 font-mono text-xs">{r.run_id}</td>
                    <td className="py-2 pr-3">{r.model_name ?? '—'}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {r.model_version != null ? `v${r.model_version}` : '—'}
                    </td>
                    <td className="py-2 pr-3 text-right tabular-nums">{num(rankIcOf(r.metrics))}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-xs">
                      {r.train_rows ?? '—'} / {r.test_rows ?? '—'}
                    </td>
                    <td className="py-2 pr-3 text-xs text-ink-dim">
                      {r.train_end ?? '—'} → {r.test_end ?? '—'}
                    </td>
                    <td className="py-2 text-xs text-ink-faint">
                      {r.created_at?.slice(0, 16) ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="信号覆盖" meta="ml_signal · 带 model_version，可审计某天是哪一版出的">
        {!status ? <Loading /> : !signals.length ? (
          <Empty>
            还没有落库信号。跑 `lq ml predict --name 模型线 --features …`。
          </Empty>
        ) : (
          <ul className="space-y-1 text-sm">
            {signals.map((s) => (
              <li key={s.name}
                  className="flex items-center justify-between border-b border-line/60 py-1.5">
                <span>{s.name}</span>
                <span className="text-xs text-ink-dim">
                  {s.days} 个交易日 · 最新 {s.last} · 版本 v{s.version}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
