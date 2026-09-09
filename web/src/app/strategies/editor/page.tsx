'use client';

/**
 * 策略编辑器 —— 左列用户策略库，右侧 CodeMirror 编辑器（动态加载，ssr:false）。
 * 保存（新建 POST / 已载入 PUT）· 校验 · 运行（run-code → 回测详情页）。
 */

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { ErrorNote } from '@/components/States';
import { get, post, putData } from '@/lib/api';
import { python } from '@codemirror/lang-python';
import { oneDark } from '@codemirror/theme-one-dark';

const CodeMirror = dynamic(() => import('@uiw/react-codemirror'), { ssr: false });

type StrategyMeta = {
  id?: string;
  name: string;
  source: 'builtin' | 'user';
  description?: string;
  version?: number;
  updated_at?: string;
};

type StrategyDetail = {
  id: string;
  name: string;
  description?: string;
  source: string;
  config?: Record<string, unknown>;
  benchmark?: string;
  version?: number;
};

type RunCodeResult = {
  run_id: string;
  n_nav_points?: number;
  n_trades?: number;
  logs?: string[];
};

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

const FACTOR_DEFAULT = 'pct_change_20';

const START_DEFAULT = '2024-01-01';
const END_DEFAULT = '2024-12-31';

export default function StrategyEditorPage() {
  const router = useRouter();
  const { data: all, mutate: mutateList } = useSWR<StrategyMeta[]>('/strategies', get);
  const userStrategies = (all ?? []).filter((s) => s.source === 'user');

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [code, setCode] = useState(DQ_TEMPLATE);
  // 载入策略时保留其原 config，保存时合并 factor_formulas，不覆盖其他键
  const [loadedConfig, setLoadedConfig] = useState<Record<string, unknown>>({});
  const [start, setStart] = useState(START_DEFAULT);
  const [end, setEnd] = useState(END_DEFAULT);
  const [formulas, setFormulas] = useState(FACTOR_DEFAULT);
  const [errors, setErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<'' | 'save' | 'validate' | 'run'>('');

  async function loadStrategy(id: string) {
    setErrors([]);
    setNotice('');
    try {
      const s = await get<StrategyDetail>(`/strategies/${id}`);
      setSelectedId(id);
      setName(s.name);
      setDescription(s.description ?? '');
      setCode(s.source || '');
      setLoadedConfig(s.config ?? {});
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
    setErrors([]);
    setNotice('');
  }

  function parseFormulas(): string[] {
    return formulas
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function save() {
    if (!selectedId && !name.trim()) {
      setErrors(['请填写策略名称']);
      return;
    }
    setBusy('save');
    setErrors([]);
    try {
      const config = { ...loadedConfig, factor_formulas: parseFormulas() };
      if (selectedId) {
        // PUT 契约：StrategySourceIn 只有 source/description/config/benchmark，不可改名
        await putData(`/strategies/${selectedId}`, { source: code, description, config });
        setNotice(`已更新「${name}」`);
      } else {
        // POST 契约：source 字段即策略代码文本（min_length=10），非 builtin/user 标识
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
      mutateList();
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy('');
    }
  }

  async function validate() {
    setBusy('validate');
    setErrors([]);
    setNotice('');
    try {
      const r = await post<{ errors: string[] }>('/strategies/validate', { source: code });
      setErrors(r.errors ?? []);
      if (!(r.errors ?? []).length) setNotice('校验通过');
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy('');
    }
  }

  async function run() {
    setBusy('run');
    setErrors([]);
    setNotice('');
    try {
      const body: Record<string, unknown> = {
        code,
        start,
        end: end || undefined,
        factor_formulas: parseFormulas(),
      };
      if (selectedId) body.strategy_id = selectedId;
      const r = await post<RunCodeResult>('/backtests/run-code', body);
      router.push(`/backtests/${r.run_id}`);
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
      setBusy('');
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="策略编辑"
        sub="Python 策略源码 · 保存到策略库 · 直接运行回测"
        actions={
          <>
            <button onClick={resetToNew} className="btn btn-sm">
              新建
            </button>
            <button onClick={validate} disabled={busy !== ''} className="btn btn-sm">
              {busy === 'validate' ? '校验中…' : '校验'}
            </button>
            <button onClick={save} disabled={busy !== ''} className="btn btn-sm btn-primary">
              {busy === 'save' ? '保存中…' : '保存'}
            </button>
            <button onClick={run} disabled={busy !== ''} className="btn btn-sm btn-accent">
              {busy === 'run' ? '运行中…' : '运行'}
            </button>
          </>
        }
      />

      <div className="flex items-stretch gap-5">
        {/* 策略库 */}
        <div className="w-64 shrink-0">
          <Panel title="策略库" meta={`用户 ${userStrategies.length}`} bodyClass="p-0">
            {!userStrategies.length ? (
              <div className="p-4 text-xs text-ink-faint">
                还没有用户策略 —— 改个名字点「保存」即可入库
              </div>
            ) : (
              <ul className="max-h-[480px] overflow-y-auto">
                {userStrategies.map((s) => (
                  <li key={s.id}>
                    <button
                      onClick={() => loadStrategy(s.id!)}
                      className={`block w-full px-4 py-2 text-left text-sm transition-colors ${
                        s.id === selectedId ? 'bg-white text-ink' : 'text-ink-dim hover:bg-white/70'
                      }`}
                    >
                      <span className="block truncate font-medium">{s.name}</span>
                      {s.description && (
                        <span className="block truncate text-xs text-ink-faint">{s.description}</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>

        {/* 编辑器 */}
        <div className="min-w-0 flex-1">
          <Panel title="策略源码" bodyClass="p-0">
            <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">策略名称</div>
                {selectedId ? (
                  <div className="input w-48 cursor-default bg-white/60 text-ink-dim" title="改名需另存为新策略">
                    {name}
                  </div>
                ) : (
                  <input value={name} onChange={(e) => setName(e.target.value)}
                         placeholder="如：双均线择时" className="input w-48" />
                )}
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">描述</div>
                <input value={description} onChange={(e) => setDescription(e.target.value)}
                       placeholder="可选" className="input w-56" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">开始日期</div>
                <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
                       className="input input-mono w-36" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">结束日期</div>
                <input type="date" value={end} onChange={(e) => setEnd(e.target.value)}
                       className="input input-mono w-36" />
              </label>
              <label className="text-sm">
                <div className="mb-1 text-xs text-ink-faint">因子公式（逗号分隔）</div>
                <input value={formulas} onChange={(e) => setFormulas(e.target.value)}
                       placeholder="pct_change_20" className="input input-mono w-64" />
              </label>
            </div>

            <div className="overflow-hidden text-sm">
              <CodeMirror
                value={code}
                height="460px"
                theme={oneDark}
                extensions={[python()]}
                onChange={(v: string) => setCode(v)}
              />
            </div>

            {errors.length > 0 && (
              <div className="border-t border-line px-4 py-3">
                {errors.map((msg, i) => (
                  <div key={i} className="mb-1 rounded-sm border border-down bg-down/5 px-3 py-2 font-mono text-xs text-down">
                    {msg}
                  </div>
                ))}
              </div>
            )}
            {notice && errors.length === 0 && (
              <div className="border-t border-line px-4 py-3 text-xs text-up">{notice}</div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
