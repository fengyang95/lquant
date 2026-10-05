'use client';

/**
 * 左侧积木面板。分组与条目**全部来自服务端算子目录** ——
 * 目录里没有的算子这里就不会出现，所以画布不可能搭出引擎不认的东西。
 *
 * 拖到画布或点击都能落块：拖拽定位顺手，点击给不习惯拖拽的场景兜底。
 *
 * 搜索框是后加的：目录实测有 45 个算子 + 7 个中缀 + 10 个字段，一面墙的按钮
 * 全靠肉眼扫太慢。搜索同时匹配中文 label 和英文算子名（hint），因为用户可能
 * 记得 `Ts_Mean` 也可能只记得「均值」。搜不到时明说去表达式直编，别让人以为
 * 是加载失败。
 */
import { useMemo, useState, type DragEvent } from 'react';

import { groupOps } from '@/lib/factor-canvas/catalog';
import type { Catalog, NodeKind } from '@/lib/factor-canvas/types';
import { BLOCK_DND_TYPE } from './Canvas';

type BlockItem = {
  key: string;
  label: string;
  hint: string;
  kind: NodeKind;
  op?: string;
  arity?: number;
};

type BlockGroup = { label: string; tone: string; blocks: BlockItem[] };

/** 分组色调对齐研报台 token：TS=金、CS=青绿、EL=靛 */
const CATEGORY_TONE: Record<string, string> = {
  TS: 'border-gold/50',
  CS: 'border-down/50',
  EL: 'border-indigo/50',
};

export default function BlockPalette({
  catalog,
  onAdd,
}: {
  catalog: Catalog;
  onAdd: (kind: NodeKind, op?: string, arity?: number) => void;
}) {
  const [query, setQuery] = useState('');

  const groups = useMemo<BlockGroup[]>(() => {
    const out: BlockGroup[] = [];

    if (catalog.fields.length > 0) {
      out.push({
        label: '数据字段',
        tone: 'border-indigo/50',
        blocks: catalog.fields.map((field) => ({
          key: `field-${field.name}`,
          label: field.label,
          hint: `$${field.name}`,
          kind: 'field' as const,
        })),
      });
    }

    out.push({
      label: '常数与参数',
      tone: 'border-line-strong',
      blocks: [{ key: 'constant', label: '数字常数', hint: '字面量', kind: 'constant' as const }],
    });

    if (catalog.infix.length > 0) {
      out.push({
        label: '四则与比较',
        tone: 'border-gold/50',
        blocks: catalog.infix.map((infix) => ({
          key: `infix-${infix.token}-${infix.arity}`,
          label: infix.label,
          hint: infix.token,
          kind: 'infix' as const,
          op: infix.token,
          arity: infix.arity,
        })),
      });
    }

    for (const group of groupOps(catalog)) {
      out.push({
        label: group.label,
        tone: CATEGORY_TONE[group.category] ?? 'border-line-strong',
        blocks: group.items.map((op) => ({
          key: `op-${op.name}`,
          label: op.label,
          hint: op.name,
          kind: 'op' as const,
          op: op.name,
        })),
      });
    }

    return out;
  }, [catalog]);

  const trimmed = query.trim().toLowerCase();

  const visible = useMemo<BlockGroup[]>(() => {
    if (!trimmed) return groups;
    return groups
      .map((group) => ({
        ...group,
        blocks: group.blocks.filter(
          (block) =>
            block.label.toLowerCase().includes(trimmed) ||
            block.hint.toLowerCase().includes(trimmed),
        ),
      }))
      .filter((group) => group.blocks.length > 0);
  }, [groups, trimmed]);

  const matchCount = useMemo(
    () => visible.reduce((sum, group) => sum + group.blocks.length, 0),
    [visible],
  );

  const startDrag = (event: DragEvent<HTMLButtonElement>, block: BlockItem) => {
    event.dataTransfer.setData(
      BLOCK_DND_TYPE,
      JSON.stringify({ kind: block.kind, op: block.op, arity: block.arity }),
    );
    event.dataTransfer.effectAllowed = 'copy';
  };

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-line">
      <div className="shrink-0 border-b border-line px-3 py-2 text-[11px] tracking-[0.16em] text-ink-faint">
        积木面板 · 拖到画布
      </div>

      {/* 搜索框固定在顶部：面板会滚动，跟着滚走就白加了 */}
      <div className="shrink-0 border-b border-line px-3 py-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') setQuery('');
          }}
          placeholder="搜算子 / 字段…"
          aria-label="搜索积木"
          className="input w-full px-2 py-1 text-xs"
        />
        {trimmed ? (
          <p className="mt-1 text-[11px] text-ink-faint">
            {matchCount > 0 ? `${matchCount} 个匹配` : '没有匹配'}
          </p>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {catalog.ops.length === 0 ? (
          <p className="border-b border-line px-3 py-2 text-[11px] leading-relaxed text-up">
            算子目录为空或未加载 —— 画布只有常数可用，请检查后端后刷新。
          </p>
        ) : null}

        {visible.map((group) => (
          <div key={group.label} className="border-b border-line px-3 py-2.5">
            <div className="mb-1.5 text-[11px] text-ink-faint">{group.label}</div>
            <div className="flex flex-wrap gap-1">
              {group.blocks.map((block) => (
                <button
                  key={block.key}
                  type="button"
                  draggable
                  onDragStart={(event) => startDrag(event, block)}
                  onClick={() => onAdd(block.kind, block.op, block.arity)}
                  title={block.hint}
                  className={`rounded-[2px] border bg-panel px-1.5 py-0.5 text-xs text-ink-dim transition-colors hover:border-up hover:text-up ${group.tone}`}
                >
                  {block.label}
                  {/* 搜索时补上算子名，否则按 `Ts_Mean` 搜出来的按钮只写「均值」，
                      用户没法确认自己搜的到底是哪个 */}
                  {trimmed ? (
                    <span className="ml-1 font-mono text-[10px] text-ink-faint">{block.hint}</span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>
        ))}

        {trimmed && matchCount === 0 ? (
          <p className="px-3 py-3 text-[11px] leading-relaxed text-ink-faint">
            目录里只有服务端注册的算子。找不到就直接在上方「DSL 表达式直编」里写，
            引擎支持的东西不必先在面板里找到。
          </p>
        ) : null}
      </div>
    </aside>
  );
}
