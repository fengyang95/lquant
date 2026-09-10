'use client';

/**
 * 回测工作台 —— 策略编辑 · 保存 · 编译运行 · 结果内联 三栏一体（Task 7 编排层）。
 * 状态全部收在本页：三个 Pane 均为纯受控组件，handler 经 @/lib/api 直连后端。
 */

import { Suspense, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import { ErrorNote, Loading, Msg } from '@/components/States';
import { del, get, post, putData } from '@/lib/api';
import RunBar from './workspace/RunBar';
import StrategyPane from './workspace/StrategyPane';
import EditorPane from './workspace/EditorPane';
import ResultPane from './workspace/ResultPane';
import QuickRunPanel from './workspace/QuickRunPanel';
import HistoryPanel from './workspace/HistoryPanel';
import {
  buildRunPayload,
  isDirty,
  parseFormulas,
  type EditorParams,
  type Snapshot,
  type StrategyMeta,
} from './workspace/state';

type StrategyDetail = {
  id?: string;
  name: string;
  description?: string;
  source?: string;
  config?: Record<string, unknown>;
};

type RunCodeResult = { run_id: string };

const DQ_TEMPLATE = `# 双均线择时示例 —— lquant 用户策略
# 可用 API: initialize / handle_data / run_daily / order / order_target_value
#           record(**kv) / get_factor_values(formula, security_list, count) / log.info

FACTOR = 'pct_change_20'


def initialize(context):
    context.security = '000300.SH'
    context.count = 20
    # 每日开盘前运行
    run_daily(before_open, time='before_open')
    log.info('策略初始化完成')


def before_open(context):
    # 拉取因子序列，计算动量
    rows = get_factor_values(
        formula=FACTOR,
        security_list=[context.security],
        count=context.count,
    )
    vals = rows.get(context.security) or []
    context.momentum = vals[-1] if vals else 0.0
    record(momentum=context.momentum)


def handle_data(context):
    # 动量为正持有，为负清仓（T+1 开盘价撮合）
    if context.momentum > 0:
        order_target_value(context.security, context.portfolio.total_value)
    else:
        order_target_value(context.security, 0)
`;

const START_DEFAULT = '2024-01-01';
const END_DEFAULT = '2024-12-31';
const FACTOR_DEFAULT = 'pct_change_20';

const PARAMS_DEFAULT: EditorParams = {
  start: START_DEFAULT,
  end: END_DEFAULT,
  formulas: FACTOR_DEFAULT,
};

const EMPTY_SNAPSHOT: Snapshot = {
  name: '',
  description: '',
  code: '',
  params: { start: '', end: '', formulas: '' },
};

type TabId = 'workspace' | 'quick' | 'history';

const TABS: { id: TabId; label: string }[] = [
  { id: 'workspace', label: '策略回测' },
  { id: 'quick', label: '快速回测' },
  { id: 'history', label: '历史与对比' },
];

function BacktestWorkspace() {
  const {
    data: allStrategies,
    mutate: mutateStrategies,
  } = useSWR<StrategyMeta[]>('/strategies', get);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [code, setCode] = useState(DQ_TEMPLATE);
  const [params, setParams] = useState<EditorParams>(PARAMS_DEFAULT);
  const [loadedConfig, setLoadedConfig] = useState<Record<string, unknown>>({});
  const [base, setBase] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState<'' | 'save' | 'validate' | 'run'>('');
  const [notice, setNotice] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>('workspace');
  const searchParams = useSearchParams();

  const currentSnapshot = (): Snapshot => ({
    name,
    description,
    code,
    params: { ...params },
  });

  /** 载入策略：填充全部字段 + 基准快照 + 原 config（保存时合并 factor_formulas） */
  async function loadStrategy(id: string) {
    setErrors([]);
    setNotice('');
    try {
      const s = await get<StrategyDetail>(`/strategies/${id}`);
      setSelectedId(id);
      setName(s?.name ?? '');
      setDescription(s?.description ?? '');
      setCode(s?.source || '');
      setLoadedConfig(s?.config ?? {});
      setBase({
        name: s?.name ?? '',
        description: s?.description ?? '',
        code: s?.source || '',
        params: { ...params },
      });
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    }
  }

  /** 从历史运行载入代码：新建态回填 code，无基准快照（视为已修改） */
  async function loadRunCode(id: string) {
    setErrors([]);
    setNotice('');
    try {
      const info = await get<{ run_id: string; code: string | null }>(
        `/backtests/${id}/code`,
      );
      setSelectedId(null);
      setName('');
      setDescription('');
      setCode(info?.code ?? '');
      setLoadedConfig({});
      setBase(null);
      setRunId(id);
      setTab('workspace');
      setNotice(`已从运行 ${id} 载入策略代码`);
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    }
  }

  function resetToNew() {
    setSelectedId(null);
    setName('');
    setDescription('');
    setCode(DQ_TEMPLATE);
    setLoadedConfig({});
    setBase(null);
    setErrors([]);
    setNotice('');
  }

  async function handleSave() {
    if (!selectedId && !name.trim()) {
      setErrors(['请填写策略名称']);
      return;
    }
    setBusy('save');
    setErrors([]);
    try {
      const config = { ...loadedConfig, factor_formulas: parseFormulas(params.formulas) };
      if (selectedId) {
        // PUT 契约：StrategySourceIn 只有 source/description/config，不可改名
        await putData(`/strategies/${selectedId}`, { source: code, description, config });
        setNotice(`已更新「${name}」`);
      } else {
        const row = await post<StrategyMeta>('/strategies', {
          name,
          source: code,
          description,
          config,
        });
        if (!row?.id) {
          setErrors(['保存接口未返回策略 id，请检查后端响应']);
          return;
        }
        setSelectedId(row.id);
        setNotice(`已创建「${name}」`);
      }
      setBase(currentSnapshot());
      void mutateStrategies();
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy('');
    }
  }

  async function handleValidate() {
    setBusy('validate');
    setErrors([]);
    setNotice('');
    try {
      const r = await post<{ errors: string[] }>('/strategies/validate', { source: code });
      const errs = r.errors ?? [];
      setErrors(errs);
      if (!errs.length) setNotice('校验通过');
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy('');
    }
  }

  async function handleRun() {
    setBusy('run');
    setErrors([]);
    setNotice('');
    try {
      const payload = buildRunPayload(code, params, selectedId);
      const r = await post<RunCodeResult>('/backtests/run-code', payload);
      setRunId(r.run_id);
      setTab('workspace');
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy('');
    }
  }

  async function handleDeleteStrategy(id: string, strategyName: string) {
    setErrors([]);
    setNotice('');
    try {
      await del(`/strategies/${id}`);
      void mutateStrategies();
      if (id === selectedId) resetToNew();
      setNotice(`已删除「${strategyName}」`);
      return;
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    }
  }

  // 路由参数：?id → 载入策略；?run → 载入运行代码。仅首次挂载执行一次。
  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    const id = searchParams.get('id');
    const run = searchParams.get('run');
    if (id) void loadStrategy(id);
    else if (run) void loadRunCode(run);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const snapshot: Snapshot = { name, description, code, params };
  const dirty = isDirty(snapshot, base);

  return (
    <div className="space-y-5">
      <PageHeader
        title="回测工作台"
        sub="编辑 · 保存 · 编译运行 · 结果内联"
        actions={
          <RunBar
            dirty={dirty}
            busy={busy}
            onNew={resetToNew}
            onSave={() => void handleSave()}
            onValidate={() => void handleValidate()}
            onRun={() => void handleRun()}
          />
        }
      />

      {/* 顶部一级 Tab */}
      <div className="flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`btn btn-sm ${tab === t.id ? 'btn-primary' : ''}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {(errors.length > 0 || notice) && (
        <div className="space-y-2">
          {errors.map((e, i) => (
            <ErrorNote key={i}>{e}</ErrorNote>
          ))}
          {notice && <Msg text={`✓ ${notice}`} />}
        </div>
      )}

      {tab === 'workspace' && (
        <div className="flex items-stretch gap-5">
          {/* 左栏：策略库 */}
          <div className="w-64 shrink-0">
            <StrategyPane
              strategies={(allStrategies ?? []).filter((s) => s.source === 'user')}
              selectedId={selectedId}
              onLoad={(id) => void loadStrategy(id)}
              onDelete={(id, n) => void handleDeleteStrategy(id, n)}
              onNew={resetToNew}
            />
          </div>
          {/* 中栏：编辑器 */}
          <div className="min-w-0 flex-1">
            <EditorPane
              name={name}
              description={description}
              code={code}
              params={params}
              selectedId={selectedId}
              onChange={(next) => {
                setName(next.name);
                setDescription(next.description);
                setCode(next.code);
                setParams(next.params);
              }}
            />
          </div>
          {/* 右栏：结果 */}
          <div className="w-[380px] shrink-0">
            <ResultPane runId={runId} />
          </div>
        </div>
      )}

      {tab === 'quick' && <QuickRunPanel />}
      {tab === 'history' && <HistoryPanel onLoadRun={(row) => void loadRunCode(row.run_id)} />}
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading>加载中…</Loading>}>
      <BacktestWorkspace />
    </Suspense>
  );
}
