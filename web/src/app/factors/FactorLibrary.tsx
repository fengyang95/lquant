'use client';

/**
 * 因子库 —— 已注册因子的两层分类管理：
 * 一级 = 来源（全部/qlib/yaml/manual/mined），组内按类别（category）分组展示。
 * 注册表单也收进这里（DSL 注册属于因子库操作）。
 */

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { Panel, Stat } from '@/components/Panel';
import { Empty, Msg } from '@/components/States';
import { get, post } from '@/lib/api';

export type FactorRow = {
  name: string; expression: string; description: string; created_at: string;
  source?: string; ic_neutral?: number | null; category?: string;
};

const SOURCE_TABS = ['全部', 'qlib', 'yaml', 'manual', 'mined'] as const;

export default function FactorLibrary({
  factors, mutate, onPickFormula,
}: {
  factors: FactorRow[] | undefined;
  mutate: () => void;
  onPickFormula?: (name: string) => void;
}) {
  const [sourceTab, setSourceTab] = useState<string>('全部');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState<'' | 'reg' | 'seed'>('');
  const [msg, setMsg] = useState('');

  // 注册表单
  const [name, setName] = useState('mom20');
  const [expression, setExpression] = useState('Rank(Ts_Mean($close,5)/$close-1)');

  const { data: builtin } = useSWR<{ name: string; family: string; formula: string }[]>(
    '/factors/builtin', get);

  async function register() {
    setBusy('reg');
    setMsg('');
    try {
      await post('/factors', { name, expression, description: '' });
      setMsg('✓ 已注册');
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function seed(kind: 'builtin' | 'yaml') {
    setBusy('seed');
    setMsg('');
    try {
      const r = await post<{ seeded: number }>(
        kind === 'builtin' ? '/factors/seed-builtin' : '/factors/seed-yaml', {});
      setMsg(`✓ 已入库 ${r.seeded} 个${kind === 'builtin' ? ' Qlib 内置因子' : ' YAML 自定义因子'}`);
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (factors ?? []).filter((f) => {
      if (sourceTab !== '全部' && (f.source ?? 'manual') !== sourceTab) return false;
      if (!q) return true;
      return f.name.toLowerCase().includes(q)
        || (f.expression ?? '').toLowerCase().includes(q);
    });
  }, [factors, sourceTab, query]);

  // 组内按 category 分组（保持稳定顺序：先按类别名排序，同名内部按名称）
  const grouped = useMemo(() => {
    const map = new Map<string, FactorRow[]>();
    for (const f of shown) {
      const cat = f.category || '自定义';
      if (!map.has(cat)) map.set(cat, []);
      map.get(cat)!.push(f);
    }
    return [...map.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], 'zh'))
      .map(([cat, rows]) => [cat, rows.sort((a, b) => a.name.localeCompare(b.name))] as const);
  }, [shown]);

  return (
    <Panel
      title="因子库"
      meta={`共 ${(factors ?? []).length} 个 · ${sourceTab} ${shown.length} 个`}
      actions={
        <>
          <button onClick={() => seed('builtin')} disabled={busy === 'seed'} className="btn btn-sm">
            {busy === 'seed' ? '入库中…' : '一键入库内置因子'}
          </button>
          <button onClick={() => seed('yaml')} disabled={busy === 'seed'} className="btn btn-sm">
            导入 YAML 因子
          </button>
        </>
      }
    >
      {/* 一级：来源 Tab */}
      <div className="mb-3 flex flex-wrap items-center gap-1">
        {SOURCE_TABS.map((src) => (
          <button
            key={src}
            onClick={() => setSourceTab(src)}
            className={`tag ${(sourceTab === src) ? 'tag-on' : ''}`}
          >
            {src}
          </button>
        ))}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索名称 / 表达式"
          className="input input-mono ml-2 w-56 py-1 text-xs"
        />
      </div>

      <Msg text={msg} />

      {/* 二级：类别分组 */}
      {shown.length === 0 ? (
        <Empty>该来源暂无因子 —— 用下方表单注册，或一键入库内置因子</Empty>
      ) : (
        <div className="space-y-4">
          {grouped.map(([cat, rows]) => (
            <div key={cat}>
              <div className="mb-1 flex items-baseline gap-2 border-b border-line pb-1">
                <span className="text-[13px] font-semibold">{cat}</span>
                <span className="text-xs text-ink-faint">{rows.length} 个</span>
              </div>
              <table className="table-dense">
                <thead>
                  <tr>
                    <th className="text-left">名称</th>
                    <th className="text-left">表达式</th>
                    <th className="text-left">来源</th>
                    <th className="text-left">IC(中性化)</th>
                    <th className="text-left">注册时间</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((f) => {
                    const icn = f.ic_neutral;
                    return (
                      <tr key={f.name} className="hover:bg-white">
                        <td className="font-medium">
                          <a href={`/factors/${f.name}`} className="hover:underline">{f.name}</a>
                          {onPickFormula && (
                            <button
                              title="填入快速评价"
                              onClick={() => onPickFormula(f.name)}
                              className="ml-2 text-xs text-ink-faint hover:text-indigo"
                            >
                              ▶ 评价
                            </button>
                          )}
                        </td>
                        <td className="font-mono text-xs text-ink-dim">{f.expression || '—'}</td>
                        <td className="text-ink-faint">{f.source ?? 'manual'}</td>
                        <td className="font-mono">{icn == null ? '—' : Number(icn).toFixed(4)}</td>
                        <td className="text-ink-faint">{f.created_at?.slice(0, 19)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {/* 注册表单 */}
      <div className="mt-5 border-t border-line pt-4">
        <div className="mb-2 text-[13px] font-semibold">注册新因子（DSL，AST 校验）</div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="因子名"
            className="input w-44"
          />
          <input
            value={expression}
            onChange={(e) => setExpression(e.target.value)}
            placeholder="Rank(Ts_Mean($close,5)/$close-1)"
            className="input input-mono min-w-72 flex-1"
          />
          <button onClick={register} disabled={busy === 'reg' || !name} className="btn btn-primary">
            {busy === 'reg' ? '注册中…' : '注册'}
          </button>
        </div>
      </div>
    </Panel>
  );
}
