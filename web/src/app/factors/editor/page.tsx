'use client';

/**
 * 因子编辑画布。
 *
 * 产物是 lquant DSL 表达式 —— 画布只负责把 DAG 说成表达式，计算、校验、
 * 评价全部走服务端既有链路（/factors/validate、/factors/evaluate、FactorEngine），
 * 不引入第二套执行语义。
 */
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useState } from 'react';

import Chart from '@/components/Chart';
import PageHeader from '@/components/PageHeader';
import { Panel, Stat } from '@/components/Panel';
import { ErrorNote, Msg } from '@/components/States';
import { ApiError, get, post } from '@/lib/api';
import { C, axes, legend, tooltip } from '@/lib/chart';

import BlockPalette from './BlockPalette';
import Inspector from './Inspector';
import OpenFactorDialog, { type FactorListItem } from './OpenFactorDialog';
import { useFactorEditor } from './useFactorEditor';

// React Flow 要量 DOM 尺寸，必须关掉 SSR（与 backtests 的 CodeMirror 同处理）
const Canvas = dynamic(() => import('./Canvas'), { ssr: false });

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
const POLL_MS = 3000;
/** 轮询上限：3s × 100 ≈ 5 分钟，超时如实报错而不是无限"运行中" */
const MAX_POLLS = 100;

type EvalMetrics = {
  ic_mean?: number | null;
  rank_ic_mean?: number | null;
  icir?: number | null;
  series?: { ic?: { dates: string[]; cum_ic: number[] } };
};

/** 内置 / 配置来源的因子是种子数据，不能原地覆盖 —— 保存时强制换名 */
const SEEDED_SOURCES = new Set(['qlib', 'yaml']);

export default function FactorEditorPage() {
  const api = useFactorEditor();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  // 打开种子因子时记下原名：只有「名字与种子同名」才拦保存。
  // 早先只看 source 判断，导致自动改名后仍被判成覆盖，保存按钮永久禁用。
  const [seedName, setSeedName] = useState<string | null>(null);
  const [openDialog, setOpenDialog] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [loadWarnings, setLoadWarnings] = useState<string[]>([]);
  const [loadTranslated, setLoadTranslated] = useState(false);
  const [openError, setOpenError] = useState('');

  const [evalJob, setEvalJob] = useState<{ id: string; factor: string } | null>(null);
  const [evalResult, setEvalResult] = useState<EvalMetrics | null>(null);
  const [evalError, setEvalError] = useState('');

  const expression = api.compiled.expression;
  const nameValid = NAME_PATTERN.test(name);
  const clobbersSeed = seedName !== null && name === seedName;
  const canSave = api.canSave && nameValid && !clobbersSeed && !busy;

  // 评价任务轮询：404 = 还没跑完，继续等；拿到结果就停。
  // 必须有次数上限：任务若在服务端抛错，结果接口会永远 404（只有成功路径落结果），
  // 没有上限就会一直显示"运行中"骗人。
  useEffect(() => {
    if (!evalJob || evalResult) return;
    let alive = true;
    let attempts = 0;
    const stop = (error: string) => {
      if (!alive) return;
      setEvalJob(null);
      setEvalError(error);
    };
    const tick = async () => {
      attempts += 1;
      if (attempts > MAX_POLLS) {
        stop(`评价任务 ${evalJob.id} 超时未出结果，请到「任务管理」查看`);
        return;
      }
      try {
        const result = await get<EvalMetrics>(`/factors/evaluate/${evalJob.id}`);
        if (alive) setEvalResult(result);
      } catch (e: unknown) {
        // 只有 404 代表"任务还没跑完"；其他错误直接如实报出来
        if (e instanceof ApiError && e.status === 404) return;
        stop(`评价结果读取失败：${e instanceof Error ? e.message : String(e)}`);
      }
    };
    const handle = setInterval(tick, POLL_MS);
    void tick();
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, [evalJob, evalResult]);

  const handleOpen = useCallback(
    async (factor: FactorListItem) => {
      setOpenDialog(false);
      setMessage('');
      setOpenError('');
      try {
        const { warnings, translated } = await api.loadExpression(factor.expression);
        setLoadWarnings(warnings);
        setLoadTranslated(translated);
        // 种子因子换个名字，避免把 qlib/yaml 的批量种子覆盖掉
        const isSeed = SEEDED_SOURCES.has(factor.source);
        setSeedName(isSeed ? factor.name : null);
        setName(isSeed ? `${factor.name}_copy` : factor.name);
        setDescription(factor.description ?? '');
        setEvalJob(null);
        setEvalResult(null);
      } catch (e: unknown) {
        // 到这一步说明连统一引擎的兼容翻译都救不回来（真语法错 / 未知字段 / 未注册算子）。
        // 如实说明原因，不要假装画布能打开。
        setLoadWarnings([]);
        setLoadTranslated(false);
        setOpenError(
          `打开 ${factor.name} 失败：${e instanceof Error ? e.message : String(e)}`,
        );
      }
    },
    [api],
  );

  const handleSave = useCallback(async (): Promise<boolean> => {
    if (!canSave) return false;
    setBusy(true);
    setMessage('');
    try {
      await post('/factors', { name, expression, description });
      api.markSaved(expression);
      setSeedName(null); // 存过一次后就是自己的因子了，同名额再存不算覆盖种子
      setMessage(`✓ 已保存 ${name}`);
      return true;
    } catch (e: unknown) {
      setMessage(`✗ ${e instanceof Error ? e.message : String(e)}`);
      return false;
    } finally {
      setBusy(false);
    }
  }, [api, canSave, description, expression, name]);

  const handleEvaluate = useCallback(async () => {
    setEvalError('');
    const saved = await handleSave();
    if (!saved) return;
    setEvalResult(null);
    try {
      const r = await post<{ job_id: string }>('/factors/evaluate', {
        factor: name,
        formula: expression,
      });
      setEvalJob({ id: r.job_id, factor: name });
      setMessage(`✓ 已入队评价任务 ${r.job_id}，完成后自动刷新`);
    } catch (e: unknown) {
      setEvalError(e instanceof Error ? e.message : String(e));
    }
  }, [expression, handleSave, name]);

  const icChart = useMemo(() => {
    const ic = evalResult?.series?.ic;
    if (!ic?.dates?.length) return null;
    return {
      grid: { left: 48, right: 16, top: 26, bottom: 28 },
      legend: legend({ top: 0, right: 0 }),
      tooltip,
      ...axes({ data: ic.dates }),
      series: [
        {
          name: '累计 IC',
          type: 'line' as const,
          data: ic.cum_ic,
          showSymbol: false,
          lineStyle: { width: 1.5, color: C.indigo },
          itemStyle: { color: C.indigo },
        },
      ],
    };
  }, [evalResult]);

  const fmt = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(4));

  return (
    <div className="space-y-5">
      <PageHeader
        title="因子编辑"
        sub="积木画布 → lquant DSL，计算与校验走统一引擎"
        actions={
          <>
            <button type="button" className="btn" onClick={() => setOpenDialog(true)}>
              打开已有因子
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                api.reset();
                setName('');
                setDescription('');
                setSeedName(null);
                setLoadWarnings([]);
                setLoadTranslated(false);
                setOpenError('');
                setEvalJob(null);
                setEvalResult(null);
                setMessage('');
              }}
            >
              新建
            </button>
            <button type="button" className="btn btn-primary" disabled={!canSave} onClick={handleSave}>
              {busy ? '保存中…' : '保存因子'}
            </button>
          </>
        }
      />

      {api.catalogError ? (
        <ErrorNote>
          算子目录加载失败：{api.catalogError} —— 画布此刻无法新建积木，请刷新重试。
        </ErrorNote>
      ) : null}

      {openError ? <ErrorNote>{openError}</ErrorNote> : null}

      {/* 表达式与校验状态 */}
      <Panel bodyClass="px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="shrink-0 text-xs text-ink-faint">DSL 表达式</span>
          <code className="min-w-0 flex-1 truncate font-mono text-[13px] text-ink">
            {expression || '（画布还没连到因子输出）'}
          </code>
          {api.checking ? (
            <span className="shrink-0 text-xs text-ink-faint">校验中…</span>
          ) : api.validation ? (
            <span className={`shrink-0 text-xs ${api.validation.ok ? 'text-down' : 'text-up'}`}>
              {api.validation.ok ? '✓ 引擎可解析' : `✗ ${api.validation.error}`}
            </span>
          ) : null}
        </div>
        {api.blockingWarnings.length > 0 ? (
          <ul className="mt-2 space-y-0.5 border-l-2 border-up pl-2 text-xs text-up">
            {api.blockingWarnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : null}
        {loadWarnings.length > 0 ? (
          <ul className="mt-2 space-y-0.5 border-l-2 border-gold pl-2 text-xs text-gold">
            {loadWarnings.map((w) => (
              <li key={w}>打开时的近似：{w}</li>
            ))}
          </ul>
        ) : null}
        {loadTranslated ? (
          <p className="mt-2 border-l-2 border-gold pl-2 text-xs text-gold">
            该因子库里存的是历史 qlib 写法，已按统一引擎翻译成 lquant DSL 打开；
            保存后以 DSL 存储。
          </p>
        ) : null}
      </Panel>

      {/* 画布三栏 */}
      <div className="flex h-[calc(100vh-330px)] min-h-[520px] border border-line bg-panel">
        <BlockPalette
          catalog={api.catalog}
          onAdd={(kind, op, arity) => api.addBlock(kind, op, undefined, arity)}
        />
        <div className="min-w-0 flex-1">
          <Canvas
            catalog={api.catalog}
            nodes={api.state.nodes}
            edges={api.state.edges}
            selectedId={api.state.selectedId}
            onMove={api.move}
            onSelect={api.select}
            onRemoveNode={api.removeBlock}
            onRemoveEdge={api.disconnect}
            onConnect={api.connect}
            onPatch={api.patchNode}
            onAddBlock={api.addBlock}
          />
        </div>
        <Inspector
          catalog={api.catalog}
          node={api.selected}
          nodes={api.state.nodes}
          edges={api.state.edges}
          onPatch={api.patchNode}
          onSetParam={api.setParam}
          onRemove={api.removeBlock}
        />
      </div>

      {/* 保存与评价 */}
      <Panel title="保存与评价">
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="因子名（字母/下划线开头）"
            className="input w-52"
          />
          <input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="备注（可选）"
            className="input min-w-52 flex-1"
          />
          <button type="button" className="btn btn-primary" disabled={!canSave} onClick={handleSave}>
            {busy ? '保存中…' : '保存因子'}
          </button>
          <button type="button" className="btn" disabled={!canSave} onClick={handleEvaluate}>
            保存并评价
          </button>
        </div>
        {clobbersSeed ? (
          <p className="mt-2 text-xs text-up">
            这是内置 / 配置来源的种子因子，同名额保存会覆盖它 —— 换个名字再存。
          </p>
        ) : null}
        {name && !nameValid ? (
          <p className="mt-2 text-xs text-up">因子名需以字母或下划线开头，仅含字母数字下划线</p>
        ) : null}
        {message ? (
          <div className="mt-2">
            <Msg text={message} />
          </div>
        ) : null}

        {evalError ? (
          <div className="mt-3">
            <ErrorNote>评价失败：{evalError}</ErrorNote>
          </div>
        ) : null}

        {evalJob && !evalResult ? (
          <p className="mt-3 text-xs text-ink-faint">
            评价任务 {evalJob.id} 运行中…（结果出来后自动显示）
          </p>
        ) : null}

        {evalResult ? (
          <div className="mt-3 space-y-3">
            <div className="flex gap-8 border-t border-line pt-3">
              <Stat label="IC 均值" value={fmt(evalResult.ic_mean)} />
              <Stat label="RankIC 均值" value={fmt(evalResult.rank_ic_mean)} />
              <Stat label="ICIR" value={fmt(evalResult.icir)} />
            </div>
            {icChart ? <Chart option={icChart} height={220} /> : null}
          </div>
        ) : null}
      </Panel>

      <OpenFactorDialog open={openDialog} onClose={() => setOpenDialog(false)} onPick={handleOpen} />
    </div>
  );
}
