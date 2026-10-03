'use client';

/**
 * 积木画布：React Flow 渲染，视觉走「研报台」token。
 *
 * 单一真相源在 useFactorEditor 的 state 里，这里只做形状映射 ——
 * 拖拽/连线/删除都直接写回编辑器状态，不留第二份画布数据。
 */
import '@xyflow/react/dist/style.css';

import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react';
import { useCallback, useMemo, type DragEvent } from 'react';

import { findField, findInfix, findOp, inputArity } from '@/lib/factor-canvas/catalog';
import { missingPorts } from '@/lib/factor-canvas/state';
import type { CanvasEdge, CanvasNode, Catalog, NodeKind } from '@/lib/factor-canvas/types';

type FactorNodeData = {
  node: CanvasNode;
  catalog: Catalog;
  missing: number[];
  onPatch: (id: string, patch: Partial<CanvasNode>) => void;
};

type FactorRFNode = Node<FactorNodeData, 'factor'>;

/** 分类配色：直接映射研报台 token，不引入第二套色板 */
const ACCENT: Record<string, string> = {
  TS: 'bg-gold',
  CS: 'bg-down',
  EL: 'bg-indigo',
  data: 'bg-indigo',
  constants: 'bg-flat',
  infix: 'bg-gold',
  output: 'bg-up',
};

function accentOf(catalog: Catalog, node: CanvasNode): string {
  if (node.kind === 'field') return ACCENT.data;
  if (node.kind === 'constant') return ACCENT.constants;
  if (node.kind === 'output') return ACCENT.output;
  if (node.kind === 'infix') return ACCENT.infix;
  return ACCENT[findOp(catalog, node.op)?.category ?? 'EL'] ?? ACCENT.EL;
}

/** 端口语义名：算子用服务端参数名，中缀用左右 */
function portLabel(catalog: Catalog, node: CanvasNode, index: number, arity: number): string {
  if (node.kind === 'infix') return arity === 1 ? '输入' : index === 0 ? '左' : '右';
  if (node.kind === 'output') return '因子值';
  if (arity === 1) return '序列';
  return `序列 ${index + 1}`;
}

function FactorNode({ data, selected }: NodeProps<FactorRFNode>) {
  const { node, catalog, missing } = data;
  const arity = inputArity(catalog, node);
  const title =
    node.kind === 'field'
      ? findField(catalog, node.field)?.label ?? node.field ?? '未选字段'
      : node.kind === 'constant'
        ? String(node.value ?? 0)
        : node.kind === 'output'
          ? node.label ?? '因子输出'
          : node.kind === 'infix'
            ? findInfix(catalog, node.op, node.arity ?? 2)?.label ?? node.op ?? '运算'
            : findOp(catalog, node.op)?.label ?? node.op ?? '未注册算子';

  const semantic =
    node.kind === 'op'
      ? node.op
      : node.kind === 'infix'
        ? `${node.op}（${node.arity ?? 2} 元）`
        : node.kind === 'field'
          ? `$${node.field}`
          : undefined;

  const params = Object.entries(node.params ?? {});

  return (
    <div
      className={`w-[190px] rounded-[2px] border bg-panel transition-colors ${
        selected ? 'border-ink' : 'border-line-strong'
      }`}
    >
      <div className="flex items-stretch">
        <span className={`w-[3px] shrink-0 ${accentOf(catalog, node)}`} aria-hidden />
        <div className="min-w-0 flex-1 px-2.5 py-2">
          <div className="truncate text-[13px] font-semibold text-ink">{title}</div>
          {semantic ? (
            <div className="mt-0.5 truncate font-mono text-[11px] text-ink-faint">{semantic}</div>
          ) : null}
          {params.length > 0 ? (
            <div className="mt-1 flex flex-wrap gap-x-2 font-mono text-[11px] text-ink-dim">
              {params.map(([name, value]) => (
                <span key={name}>
                  {name}={value}
                </span>
              ))}
            </div>
          ) : null}
          {node.kind === 'op' && node.op === undefined ? (
            <div className="mt-1 text-[11px] text-up">未在算子目录中，请删除</div>
          ) : null}
        </div>
      </div>

      {missing.length > 0 ? (
        <div className="border-t border-line px-2.5 py-1 text-[11px] text-up">
          缺 {missing.map((i) => portLabel(catalog, node, i, arity)).join('、')}
        </div>
      ) : null}

      {arity > 0
        ? Array.from({ length: arity }, (_, index) => (
            <Handle
              key={index}
              id={`in-${index}`}
              type="target"
              position={Position.Left}
              style={{ top: `${((index + 1) / (arity + 1)) * 100}%` }}
              className="!h-2 !w-2 !border !border-line-strong !bg-paper"
            />
          ))
        : null}

      {node.kind !== 'output' ? (
        <Handle
          id="out"
          type="source"
          position={Position.Right}
          className="!h-2 !w-2 !border !border-line-strong !bg-ink"
        />
      ) : null}
    </div>
  );
}

const nodeTypes = { factor: FactorNode };

type CanvasProps = {
  catalog: Catalog;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  selectedId: string | null;
  onMove: (id: string, x: number, y: number) => void;
  onSelect: (id: string | null) => void;
  onRemoveNode: (id: string) => void;
  onRemoveEdge: (id: string) => void;
  onConnect: (edge: CanvasEdge) => void;
  onPatch: (id: string, patch: Partial<CanvasNode>) => void;
  /** 从左侧面板拖进来的积木 */
  onAddBlock: (
    kind: NodeKind,
    opName: string | undefined,
    at: { x: number; y: number },
    arity?: number,
  ) => void;
};

/** 拖拽面板积木时挂在 dataTransfer 上的类型 */
export const BLOCK_DND_TYPE = 'application/lquant-factor-block';

function CanvasInner({
  catalog,
  nodes,
  edges,
  selectedId,
  onMove,
  onSelect,
  onRemoveNode,
  onRemoveEdge,
  onConnect,
  onPatch,
  onAddBlock,
}: CanvasProps) {
  const { screenToFlowPosition } = useReactFlow();
  const rfNodes = useMemo<FactorRFNode[]>(
    () =>
      nodes.map((node) => ({
        id: node.id,
        type: 'factor' as const,
        position: { x: node.x ?? 0, y: node.y ?? 0 },
        selected: node.id === selectedId,
        data: {
          node,
          catalog,
          missing: missingPorts(catalog, node, edges),
          onPatch,
        },
      })),
    [nodes, edges, catalog, selectedId, onPatch],
  );

  const rfEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: 'out',
        targetHandle: `in-${edge.targetPort}`,
        style: { stroke: '#94989F', strokeWidth: 1.2 },
      })),
    [edges],
  );

  const handleNodesChange = useCallback(
    (changes: NodeChange<FactorRFNode>[]) => {
      // 一批变更里可能同时含「旧节点取消选中」与「新节点选中」，
      // 逐条 setState 会因顺序不同丢选中 —— 先聚合出最终选中项再写回。
      let selectedId: string | null = null;
      let sawSelect = false;
      for (const change of changes) {
        if (change.type === 'position' && change.position) {
          onMove(change.id, change.position.x, change.position.y);
        } else if (change.type === 'remove') {
          onRemoveNode(change.id);
        } else if (change.type === 'select') {
          sawSelect = true;
          if (change.selected) selectedId = change.id;
        }
      }
      if (sawSelect) onSelect(selectedId);
    },
    [onMove, onRemoveNode, onSelect],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange<Edge>[]) => {
      for (const change of changes) {
        if (change.type === 'remove') onRemoveEdge(change.id);
      }
    },
    [onRemoveEdge],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      const port = Number((connection.targetHandle ?? 'in-0').replace('in-', ''));
      onConnect({
        id: `${connection.source}->${connection.target}:${port}`,
        source: connection.source,
        target: connection.target,
        targetPort: Number.isFinite(port) ? port : 0,
      });
    },
    [onConnect],
  );

  /** 连到自己的上游会成环，拖的时候就挡住（state 里还有一道兜底） */
  const isValidConnection = useCallback(
    (connection: Connection | Edge) => {
      const { source, target } = connection;
      if (!source || !target || source === target) return false;
      const reachable = new Set<string>();
      const stack = [source];
      while (stack.length > 0) {
        const current = stack.pop() as string;
        if (current === target) return false;
        if (reachable.has(current)) continue;
        reachable.add(current);
        for (const edge of edges) {
          if (edge.target === current) stack.push(edge.source);
        }
      }
      return true;
    },
    [edges],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      const raw = event.dataTransfer.getData(BLOCK_DND_TYPE);
      if (!raw) return;
      event.preventDefault();
      let payload: { kind?: NodeKind; op?: string; arity?: number };
      try {
        payload = JSON.parse(raw) as { kind?: NodeKind; op?: string; arity?: number };
      } catch {
        return; // 外来拖拽数据不解析，直接忽略
      }
      if (!payload.kind) return;
      const at = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      // 让积木中心落在指针处（节点宽 190、头高约 48）
      onAddBlock(payload.kind, payload.op, { x: at.x - 95, y: at.y - 24 }, payload.arity);
    },
    [onAddBlock, screenToFlowPosition],
  );

  return (
    <div
      className="h-full w-full"
      onDrop={handleDrop}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      }}
    >
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={handleConnect}
        isValidConnection={isValidConnection}
        onPaneClick={() => onSelect(null)}
        fitView
        minZoom={0.3}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={['Backspace', 'Delete']}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#E3E3DC" />
        <Controls
          showInteractive={false}
          className="!rounded-[2px] !border !border-line-strong !bg-panel !shadow-none"
        />
      </ReactFlow>
    </div>
  );
}

export default function Canvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

export type { NodeKind };
