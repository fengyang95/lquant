'use client';

/**
 * 左侧积木面板。分组与条目**全部来自服务端算子目录** ——
 * 目录里没有的算子这里就不会出现，所以画布不可能搭出引擎不认的东西。
 *
 * 拖到画布或点击都能落块：拖拽定位顺手，点击给不习惯拖拽的场景兜底。
 */
import { useMemo, type DragEvent } from 'react';

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

  const startDrag = (event: DragEvent<HTMLButtonElement>, block: BlockItem) => {
    event.dataTransfer.setData(
      BLOCK_DND_TYPE,
      JSON.stringify({ kind: block.kind, op: block.op, arity: block.arity }),
    );
    event.dataTransfer.effectAllowed = 'copy';
  };

  return (
    <aside className="flex w-56 shrink-0 flex-col overflow-y-auto border-r border-line">
      <div className="border-b border-line px-3 py-2 text-[11px] tracking-[0.16em] text-ink-faint">
        积木面板 · 拖到画布
      </div>
      {catalog.ops.length === 0 ? (
        <p className="border-b border-line px-3 py-2 text-[11px] leading-relaxed text-up">
          算子目录为空或未加载 —— 画布只有常数可用，请检查后端后刷新。
        </p>
      ) : null}
      {groups.map((group) => (
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
              </button>
            ))}
          </div>
        </div>
      ))}
    </aside>
  );
}
