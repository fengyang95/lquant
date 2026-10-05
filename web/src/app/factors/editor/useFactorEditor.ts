'use client';

/**
 * 因子编辑器的状态编排：目录加载、画布编辑、表达式直编、DSL 实时校验。
 *
 * 画布语义全部来自服务端目录；这里只做编排，不含算子知识。
 *
 * ## 双向同步的真相源
 *
 * 「表达式直编」加上之后，编辑器有两个可写入口：文本与画布。两者必须收敛
 * 到同一个表达式，否则用户看到的和保存的会不是一回事。这里的规则是
 * **最后改动的一方为准**：
 *
 * - 用户改文本 → 防抖请求 `/factors/ast`；解析通过就把画布重铺成这棵树，
 *   并且**保存的是服务端归一后的这段文本**。画布只是它的一个视图 ——
 *   反解析表达不了的部分（如参数写成表达式）只告警，不改写用户写的东西。
 * - 用户改画布 → 画布编译出的表达式写回文本，再走同一套校验。
 *
 * 两条链路都用「结论属于哪段文本」标记（`SyncState.expression`）来防止
 * 旧结论放行新表达式 —— 表达式改了、校验还没回来时，保存必须被挡住。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { post } from '@/lib/api';
import { EMPTY_CATALOG, fetchCatalog } from '@/lib/factor-canvas/catalog';
import { compileCanvas } from '@/lib/factor-canvas/compile';
import { decompileAst } from '@/lib/factor-canvas/decompile';
import { layoutNodes } from '@/lib/factor-canvas/layout';
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
  AstNode,
  AstResponse,
  CanvasEdge,
  CanvasNode,
  Catalog,
  LoadExpressionResult,
  NodeKind,
} from '@/lib/factor-canvas/types';

/**
 * 表达式校验结论。
 *
 * `expression` 是这份结论**对应的文本**：表达式改了、防抖还没触发时，
 * 上一次的 ok 会被当成当前表达式的结论，用户就能保存一段从未校验过的 DSL。
 * 带上归属之后，`canSave` 才能要求"结论属于当前文本"。
 */
export type SyncState = {
  expression: string;
  checking: boolean;
  error: string | null;
  /** 服务端归一后的 DSL（可保存的表达式）；解析失败为 null */
  normalized: string | null;
  /** 服务端把历史 qlib 写法翻译成了 lquant DSL */
  translated: boolean;
  /** 画布无法完整表达的部分（有损映射）—— 只影响可视化，不影响保存 */
  canvasWarnings: string[];
};

export const EMPTY_SYNC: SyncState = {
  expression: '',
  checking: false,
  error: null,
  normalized: '',
  translated: false,
  canvasWarnings: [],
};

const PARSE_DEBOUNCE_MS = 400;

/** 新积木落点：按已放数量错位排布，避免叠在一起 */
function dropPosition(count: number): { x: number; y: number } {
  return { x: 80 + (count % 6) * 40, y: 60 + (count % 10) * 36 };
}

export function useFactorEditor() {
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [state, setState] = useState(newCanvas);
  /** 表达式直编框里的文本 —— 保存以它为准 */
  const [text, setTextState] = useState('');
  const [sync, setSync] = useState<SyncState>(EMPTY_SYNC);

  const parseSeq = useRef(0);
  /** 最近一次「文本 → 画布」铺出来的表达式；用它把反向同步认出来，避免自激 */
  const appliedRef = useRef<string | null>(null);
  /** 最后改动的一方 */
  const sourceRef = useRef<'text' | 'canvas'>('canvas');

  // 供防抖回调读取最新值，同时不把它们放进 effect 依赖（否则会自激成环）
  const syncRef = useRef(sync);
  const stateRef = useRef(state);
  const textRef = useRef(text);
  const compiledRef = useRef<{ expression: string; warnings: string[] }>({
    expression: '',
    warnings: [],
  });
  useEffect(() => {
    syncRef.current = sync;
  }, [sync]);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);
  useEffect(() => {
    textRef.current = text;
  }, [text]);

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
  useEffect(() => {
    compiledRef.current = compiled;
  }, [compiled]);

  /** 把服务端 AST 铺成画布（自动布局），返回有损映射告警 */
  const applyAstToCanvas = useCallback(
    (ast: AstNode): string[] => {
      const { nodes, edges, warnings } = decompileAst(ast, catalog);
      const laid = layoutNodes(nodes, edges, stateRef.current.nodes);
      // 记下这次铺出来的表达式：反向同步据此判断"画布这次变化是文本引起的"
      appliedRef.current = compileCanvas(laid, edges, catalog).expression;
      setState((prev) => ({ ...prev, nodes: laid, edges, selectedId: null }));
      return warnings;
    },
    [catalog],
  );

  // 画布 → 文本：画布是最后改动方时，把编译结果写回文本框。
  // appliedRef 命中说明这次变化是文本铺出来的，不能反向覆盖用户正在敲的字。
  useEffect(() => {
    if (appliedRef.current === compiled.expression) return;
    appliedRef.current = null;
    sourceRef.current = 'canvas';
    setTextState(compiled.expression);
  }, [compiled.expression]);

  // 文本 → 画布（防抖）。服务端是唯一裁判：解析 + 静态检查一次做完。
  useEffect(() => {
    const raw = text.trim();
    // 这段文本已经有结论了（打开因子时同步写入过），不重复请求
    if (syncRef.current.expression === raw) return;
    if (!raw) {
      parseSeq.current += 1;
      setSync(EMPTY_SYNC);
      return;
    }
    const seq = (parseSeq.current += 1);
    setSync((prev) => ({ ...prev, checking: true }));
    const handle = setTimeout(() => {
      void (async () => {
        try {
          const res = await post<AstResponse>('/factors/ast', { expression: raw });
          if (seq !== parseSeq.current) return; // 已有更新的输入，丢弃旧结论
          const fromText = sourceRef.current === 'text';
          const canvasWarnings = fromText ? applyAstToCanvas(res.ast) : [];
          setSync({
            expression: raw,
            checking: false,
            error: null,
            normalized: res.expression,
            translated: res.translated === true,
            canvasWarnings,
          });
        } catch (e: unknown) {
          if (seq !== parseSeq.current) return;
          setSync((prev) => ({
            expression: raw,
            checking: false,
            error: e instanceof Error ? e.message : String(e),
            normalized: null,
            translated: false,
            canvasWarnings: sourceRef.current === 'text' ? prev.canvasWarnings : [],
          }));
        }
      })();
    }, PARSE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [text, applyAstToCanvas]);

  /** 用户敲字：标记文本为最后改动方 */
  const setText = useCallback((value: string) => {
    sourceRef.current = 'text';
    setTextState(value);
  }, []);

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
    parseSeq.current += 1;
    appliedRef.current = null;
    sourceRef.current = 'canvas';
    setTextState('');
    setState(newCanvas());
    setSync(EMPTY_SYNC);
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
      const laid = layoutNodes(nodes, edges);
      parseSeq.current += 1; // 作废在途的文本解析，避免旧响应盖掉刚打开的因子
      appliedRef.current = compileCanvas(laid, edges, catalog).expression;
      sourceRef.current = 'text';
      // 文本用**服务端归一后**的表达式：历史 qlib 写法打开后画布产出的是 DSL，
      // 拿原始串当文本会一直显示旧语法，保存也会存回旧语法。
      setTextState(res.expression);
      setState(loadState(laid, edges, res.expression));
      setSync({
        expression: res.expression,
        checking: false,
        error: null,
        normalized: res.expression,
        translated: res.translated === true,
        canvasWarnings: warnings,
      });
      return { warnings, translated: res.translated === true };
    },
    [catalog, reset],
  );

  /** 保存成功后把当前表达式设为脏标记基线 */
  const markSaved = useCallback((expression: string) => {
    setState((prev) => ({ ...prev, baselineExpression: expression }));
  }, []);

  /** 归一：把文本框换成服务端归一后的规范写法 */
  const formatText = useCallback(() => {
    const normalized = syncRef.current.normalized;
    // 同样的归属判断：结论不属于当前文本时，normalized 是上一段文本的规范写法，
    // 套上去等于悄悄回退掉用户刚敲的改动。归一只是排版，不做破坏性操作。
    if (!normalized || syncRef.current.expression !== textRef.current.trim()) return;
    sourceRef.current = 'text';
    setTextState(normalized);
  }, []);

  /** 回退：丢弃当前文本，回到画布上的结构（文本解析不了时的出口） */
  const revertToCanvas = useCallback(() => {
    sourceRef.current = 'canvas';
    setTextState(compiledRef.current.expression);
  }, []);

  const selected = state.nodes.find((n) => n.id === state.selectedId) ?? null;
  /**
   * 画布自身的结构问题（没连线、缺端口、成环…）。
   *
   * 早先它叫 blockingWarnings 并且**拦保存**：那时画布是唯一真相源，画布画不出来
   * 就等于没有表达式。现在文本是真相源，只要文本能过服务端静态检查就该允许保存 ——
   * 画布表达不出来的写法（如把参数写成表达式）本来就是直编的意义所在。
   * 所以它降级为提示：说清楚画布缺什么，但不挡保存。
   */
  const canvasProblems = compiled.warnings;

  /**
   * 允许保存的条件：当前文本已解析完、无错误、结论属于**当前**文本。
   * 少了最后一条，表达式改动后防抖还没触发时，上一次的 ok 会放行
   * 一段从未校验过的 DSL —— 正是本模块最该防的静默错误。
   */
  const canSave =
    !sync.checking &&
    sync.error === null &&
    sync.expression === text.trim() &&
    (sync.normalized ?? '').trim().length > 0;

  return {
    catalog,
    catalogError,
    state,
    compiled,
    /** 表达式直编框的文本 */
    text,
    setText,
    sync,
    selected,
    canvasProblems,
    canSave,
    /** 保存用的表达式：服务端归一后的 DSL */
    saveExpression: sync.normalized ?? '',
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
    formatText,
    revertToCanvas,
  };
}

export type FactorEditorApi = ReturnType<typeof useFactorEditor>;
