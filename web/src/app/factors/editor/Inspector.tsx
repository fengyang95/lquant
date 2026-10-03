'use client';

/**
 * 右侧检查器：选中积木的参数、连接关系与服务端语义说明。
 *
 * 中文说明直接展示服务端目录里的 `label` —— 画布不自己解释算子含义，
 * 这样 Greater 是「逐元素取大」还是「比较」这类分歧不可能被前端说错。
 */
import { findInfix, findOp, inputArity, nodeTitle, paramLabel } from '@/lib/factor-canvas/catalog';
import type { CanvasEdge, CanvasNode, Catalog } from '@/lib/factor-canvas/types';

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1">
      <span className="shrink-0 text-xs text-ink-faint">{label}</span>
      <span className="min-w-0 flex-1 text-right text-xs text-ink-dim">{children}</span>
    </div>
  );
}

export default function Inspector({
  catalog,
  node,
  nodes,
  edges,
  onPatch,
  onSetParam,
  onRemove,
}: {
  catalog: Catalog;
  node: CanvasNode | null;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  onPatch: (id: string, patch: Partial<CanvasNode>) => void;
  onSetParam: (id: string, name: string, value: number) => void;
  onRemove: (id: string) => void;
}) {
  if (!node) {
    return (
      <aside className="flex w-72 shrink-0 flex-col border-l border-line">
        <div className="border-b border-line px-3 py-2 text-[11px] tracking-[0.16em] text-ink-faint">
          检查器
        </div>
        <p className="px-3 py-4 text-xs leading-relaxed text-ink-faint">
          点选画布上的积木查看参数与语义。
          <br />
          从左侧面板拖入积木，拖动端口连线。
        </p>
      </aside>
    );
  }

  const arity = inputArity(catalog, node);
  const opDef = node.kind === 'op' ? findOp(catalog, node.op) : undefined;
  const infixDef = node.kind === 'infix' ? findInfix(catalog, node.op, node.arity ?? 2) : undefined;
  const semantic = opDef?.label ?? infixDef?.label;
  const byId = new Map(nodes.map((item) => [item.id, item]));

  return (
    <aside className="flex w-72 shrink-0 flex-col overflow-y-auto border-l border-line">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <span className="text-[11px] tracking-[0.16em] text-ink-faint">检查器</span>
        <button type="button" onClick={() => onRemove(node.id)} className="btn btn-sm">
          删除
        </button>
      </div>

      <div className="border-b border-line px-3 py-3">
        <div className="text-[13px] font-semibold text-ink">{nodeTitle(catalog, node)}</div>
        <div className="mt-0.5 font-mono text-[11px] text-ink-faint">
          {opDef?.name ?? (node.kind === 'infix' ? node.op : node.kind)}
        </div>
        {semantic ? (
          <p className="mt-2 border-l-2 border-line-strong pl-2 text-xs leading-relaxed text-ink-dim">
            {semantic}
          </p>
        ) : null}
      </div>

      {/* 字段积木：从服务端字段清单里选 */}
      {node.kind === 'field' ? (
        <div className="border-b border-line px-3 py-3">
          <label className="mb-1 block text-xs text-ink-faint" htmlFor="field-select">
            字段
          </label>
          <select
            id="field-select"
            value={node.field ?? ''}
            onChange={(event) => onPatch(node.id, { field: event.target.value })}
            className="input w-full"
          >
            {catalog.fields.map((field) => (
              <option key={field.name} value={field.name}>
                {field.label}（${field.name}）
              </option>
            ))}
          </select>
        </div>
      ) : null}

      {/* 常数积木 */}
      {node.kind === 'constant' ? (
        <div className="border-b border-line px-3 py-3">
          <label className="mb-1 block text-xs text-ink-faint" htmlFor="const-value">
            数值
          </label>
          <input
            id="const-value"
            type="number"
            step="any"
            value={node.value ?? 0}
            onChange={(event) => onPatch(node.id, { value: Number(event.target.value) })}
            className="input input-mono w-full"
          />
        </div>
      ) : null}

      {/* 算子标量参数：控件由服务端声明的参数清单生成 */}
      {opDef && opDef.params.length > 0 ? (
        <div className="border-b border-line px-3 py-3">
          <div className="mb-2 text-xs text-ink-faint">参数</div>
          {opDef.params.map((param) => (
            <div key={param.name} className="mb-2 last:mb-0">
              <label className="mb-1 block text-[11px] text-ink-faint" htmlFor={`p-${param.name}`}>
                {paramLabel(param)}（{param.name}）
              </label>
              <input
                id={`p-${param.name}`}
                type="number"
                step={param.type === 'window' ? 1 : 'any'}
                min={param.type === 'window' ? 1 : undefined}
                value={node.params?.[param.name] ?? ''}
                placeholder="未设置"
                onChange={(event) => onSetParam(node.id, param.name, Number(event.target.value))}
                className="input input-mono w-full"
              />
            </div>
          ))}
        </div>
      ) : null}

      {/* 连接关系 */}
      <div className="px-3 py-3">
        <div className="mb-1 text-xs text-ink-faint">连接</div>
        {arity === 0 ? (
          <p className="text-xs text-ink-faint">无输入</p>
        ) : (
          Array.from({ length: arity }, (_, index) => {
            const edge = edges.find((e) => e.target === node.id && e.targetPort === index);
            const source = edge ? byId.get(edge.source) : undefined;
            return (
              <Row key={index} label={`输入 ${index + 1}`}>
                {source ? (
                  <span className="truncate">{nodeTitle(catalog, source)}</span>
                ) : (
                  <span className="text-up">未连接</span>
                )}
              </Row>
            );
          })
        )}
        <Row label="输出连接">
          {edges.filter((e) => e.source === node.id).length}
        </Row>
      </div>
    </aside>
  );
}
