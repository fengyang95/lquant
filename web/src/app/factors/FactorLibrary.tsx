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
import { del, get, post, put } from '@/lib/api';
import RegisterForm from './RegisterForm';

export type FactorRow = {
  name: string; expression: string; description: string; created_at: string;
  source?: string; ic_neutral?: number | null; category?: string;
};

const SOURCE_TABS = ['全部', 'qlib', 'yaml', 'manual', 'mined'] as const;

/** 可编辑来源：种子批量灌入的因子改了会被下次 seed 覆盖（后端同口径拦截） */
const EDITABLE = new Set(['manual', 'mined']);

export type EditTarget = {
  name: string; expression: string; description: string; category: string;
};

export default function FactorLibrary({
  factors, mutate, onPickFormula,
}: {
  factors: FactorRow[] | undefined;
  mutate: () => void;
  onPickFormula?: (name: string) => void;
}) {
  const [sourceTab, setSourceTab] = useState<string>('全部');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState<'' | 'seed' | 'edit' | 'del'>('');
  const [msg, setMsg] = useState('');
  // 注册表单收进 RegisterForm（算子面板 + 实时校验）
  // 编辑弹层
  const [editing, setEditing] = useState<EditTarget | null>(null);

  const { data: builtin } = useSWR<{ name: string; family: string; formula: string }[]>(
    '/factors/builtin', get);

  async function saveEdit() {
    if (!editing) return;
    setBusy('edit');
    setMsg('');
    try {
      await put(`/factors/${editing.name}`, {
        expression: editing.expression,
        description: editing.description,
        category: editing.category.trim() || null,
      });
      setMsg(`✓ 已更新 ${editing.name}`);
      setEditing(null);
      mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy('');
    }
  }

  async function remove(name: string) {
    if (!window.confirm(`确认删除因子 ${name}？历史报告与挖掘台账会保留。`)) return;
    setBusy('del');
    setMsg('');
    try {
      await del(`/factors/${name}`);
      setMsg(`✓ 已删除 ${name}`);
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
                    <th className="text-left">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((f) => {
                    const icn = f.ic_neutral;
                    const editable = EDITABLE.has(f.source ?? 'manual');
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
                        <td className="whitespace-nowrap">
                          {editable ? (
                            <>
                              <button
                                title="编辑表达式 / 描述 / 类别"
                                onClick={() => setEditing({
                                  name: f.name,
                                  expression: f.expression ?? '',
                                  description: f.description ?? '',
                                  category: f.category === '自定义' ? '' : (f.category ?? ''),
                                })}
                                className="text-xs text-indigo hover:underline"
                                disabled={busy !== ''}
                              >
                                编辑
                              </button>
                              <button
                                title="删除因子"
                                onClick={() => remove(f.name)}
                                className="ml-2 text-xs text-down hover:underline"
                                disabled={busy !== ''}
                              >
                                删除
                              </button>
                            </>
                          ) : (
                            <span className="text-xs text-ink-faint" title="种子灌入因子不可编辑">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {/* 编辑弹层（因子名不可改：它是全部关联数据的主键） */}
      {editing && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
          onClick={() => { if (busy !== 'edit') setEditing(null); }}>
          <div className="w-[560px] max-w-[92vw] border border-line bg-white p-4 shadow-lg"
            onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between">
              <span className="text-[13px] font-semibold">编辑因子 · {editing.name}</span>
              <button
                onClick={() => setEditing(null)}
                className="text-sm text-ink-faint hover:text-ink"
                disabled={busy === 'edit'}
              >
                ✕
              </button>
            </div>
            <div className="space-y-3">
              <label className="block">
                <span className="mb-1 block text-xs text-ink-dim">DSL 表达式（AST 校验，留空 = 仅评分用现算公式）</span>
                <input
                  value={editing.expression}
                  onChange={(e) => setEditing({ ...editing, expression: e.target.value })}
                  className="input input-mono w-full font-mono text-xs"
                  placeholder="Rank(Ts_Mean($close,5)/$close-1)"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-ink-dim">描述</span>
                <textarea
                  value={editing.description}
                  onChange={(e) => setEditing({ ...editing, description: e.target.value })}
                  rows={2}
                  className="input w-full text-xs"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-ink-dim">类别（留空归入「自定义」）</span>
                <input
                  value={editing.category}
                  onChange={(e) => setEditing({ ...editing, category: e.target.value })}
                  className="input w-56 text-xs"
                  placeholder="动量 / 波动率 / 量价 …"
                />
              </label>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setEditing(null)} className="btn btn-sm" disabled={busy === 'edit'}>
                取消
              </button>
              <button onClick={saveEdit} className="btn btn-sm btn-primary" disabled={busy === 'edit'}>
                {busy === 'edit' ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 注册表单（算子面板 + 实时 DSL 校验，见 RegisterForm） */}
      <div className="mt-5 border-t border-line pt-4">
        <RegisterForm onSaved={mutate} />
      </div>
    </Panel>
  );
}
