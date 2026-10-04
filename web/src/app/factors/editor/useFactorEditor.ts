'use client';

/**
 * 因子编辑器的状态编排：目录加载、画布编辑、表达式编译、DSL 实时校验。
 *
 * 画布语义全部来自服务端目录；这里只做编排，不含算子知识。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { post } from '@/lib/api';
import { EMPTY_CATALOG, fetchCatalog } from '@/lib/factor-canvas/catalog';
import { compileCanvas } from '@/lib/factor-canvas/compile';
import { decompileAst } from '@/lib/factor-canvas/decompile';
import {
  addNode,
  connect as connectEdge,
  createNode,
  disconnect as disconnectEdge,
  loadState,
  moveNode,
  newCanvas,
  nextNodeId,
  removeNode,
  selectNode,
  setParam as setNodeParam,
  updateNode,
} from '@/lib/factor-canvas/state';
import type {
  AstResponse,
  CanvasEdge,
  CanvasNode,
  Catalog,
  LoadExpressionResult,
  NodeKind,
} from '@/lib/factor-canvas/types';

/**
 * 校验结果必须**带上它校验的是哪个表达式**：否则表达式改了、防抖还没触发时，
 * 上一次的 ok 会被当成当前表达式的结论，用户就能保存一段从未校验过的 DSL。
 */
export type ValidationState = {
  expression: string;
  ok: boolean;
  error: string | null;
} | null;

const VALIDATE_DEBOUNCE_MS = 400;

/** 新积木落点：按已放数量错位排布，避免叠在一起 */
function dropPosition(count: number): { x: number; y: number } {
  return { x: 80 + (count % 6) * 40, y: 60 + (count % 10) * 36 };
}

export function useFactorEditor() {
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [state, setState] = useState(newCanvas);
  const [validation, setValidation] = useState<ValidationState>(null);
  const [checking, setChecking] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const validateSeq = useRef(0);

  useEffect(() => {
    let alive = true;
    fetchCatalog()
      .then((next) => {
        if (alive) setCatalog(next);
      })
      .catch((e: unknown) => {
        if (alive) setCatalogError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const compiled = useMemo(
    () => compileCanvas(state.nodes, state.edges, catalog),
    [state.nodes, state.edges, catalog],
  );

  // DSL 实时校验：表达式变了防抖发一次，服务端是唯一裁判。
  // 序号守卫：慢响应回来时若已有更新的请求发出，直接丢弃，避免旧结论覆盖新结论。
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const expression = compiled.expression;
    if (!expression) {
      validateSeq.current += 1; // 作废在途请求
      setValidation(null);
      setChecking(false);
      return;
    }
    timer.current = setTimeout(() => {
      const seq = (validateSeq.current += 1);
      setChecking(true);
      post<{ ok: boolean; error: string | null }>('/factors/validate', { expression })
        .then((r) => {
          if (seq === validateSeq.current) setValidation({ expression, ...r });
        })
        .catch((e: unknown) => {
          if (seq === validateSeq.current) {
            setValidation({
              expression,
              ok: false,
              error: e instanceof Error ? e.message : String(e),
            });
          }
        })
        .finally(() => {
          if (seq === validateSeq.current) setChecking(false);
        });
    }, VALIDATE_DEBOUNCE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [compiled.expression]);

  const addBlock = useCallback(
    (kind: NodeKind, opName?: string, at?: { x: number; y: number }, arity = 2) => {
      setState((prev) => {
        const position = at ?? dropPosition(prev.nodes.length);
        const node: CanvasNode = {
          ...createNode(catalog, kind, position.x, position.y, opName, arity),
          id: nextNodeId(prev.nodes, kind),
        };
        return addNode(prev, node);
      });
    },
    [catalog],
  );

  const removeBlock = useCallback((id: string) => setState((prev) => removeNode(prev, id)), []);

  const move = useCallback(
    (id: string, x: number, y: number) => setState((prev) => moveNode(prev, id, x, y)),
    [],
  );

  const connect = useCallback(
    (edge: CanvasEdge) => setState((prev) => connectEdge(prev, edge)),
    [],
  );

  const disconnect = useCallback(
    (edgeId: string) => setState((prev) => disconnectEdge(prev, edgeId)),
    [],
  );

  const setParam = useCallback(
    (id: string, name: string, value: number) =>
      setState((prev) => setNodeParam(prev, id, name, value)),
    [],
  );

  const patchNode = useCallback(
    (id: string, patch: Partial<CanvasNode>) =>
      setState((prev) => updateNode(prev, id, patch)),
    [],
  );

  const select = useCallback((id: string | null) => setState((prev) => selectNode(prev, id)), []);

  const reset = useCallback(() => {
    setState(newCanvas());
    setValidation(null);
  }, []);

  /** 用 DSL 表达式铺画布（打开已有因子 / 粘贴表达式） */
  const loadExpression = useCallback(
    async (expression: string): Promise<LoadExpressionResult> => {
      const trimmed = expression.trim();
      if (!trimmed) {
        reset();
        return { warnings: [], translated: false };
      }
      const res = await post<AstResponse>('/factors/ast', { expression: trimmed });
      const { nodes, edges, warnings } = decompileAst(res.ast, catalog);
      // 基线用服务端**归一后**的表达式：历史 qlib 写法打开后画布产出的是 DSL，
      // 拿原始 qlib 串当基线，脏标记会一直为真（打开即脏）。
      setState(loadState(nodes, edges, res.expression || trimmed));
      return { warnings, translated: res.translated === true };
    },
    [catalog, reset],
  );

  /** 保存成功后把当前表达式设为脏标记基线 */
  const markSaved = useCallback((expression: string) => {
    setState((prev) => ({ ...prev, baselineExpression: expression }));
  }, []);

  const selected = state.nodes.find((n) => n.id === state.selectedId) ?? null;
  const blockingWarnings = compiled.warnings;

  return {
    catalog,
    catalogError,
    state,
    compiled,
    validation,
    checking,
    selected,
    blockingWarnings,
    /**
     * 允许保存的条件：无告警、校验完成、且校验结论属于**当前**表达式。
     * 少了最后一条，表达式改动后防抖还没触发时，上一次的 ok 会放行
     * 一段从未校验过的 DSL —— 正是本模块最该防的静默错误。
     */
    canSave:
      blockingWarnings.length === 0 &&
      compiled.expression.length > 0 &&
      !checking &&
      validation !== null &&
      validation.expression === compiled.expression &&
      validation.ok,
    addBlock,
    removeBlock,
    move,
    connect,
    disconnect,
    setParam,
    patchNode,
    select,
    reset,
    loadExpression,
    markSaved,
  };
}

export type FactorEditorApi = ReturnType<typeof useFactorEditor>;
