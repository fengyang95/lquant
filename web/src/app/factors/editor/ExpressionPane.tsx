'use client';

/**
 * 表达式直编框 —— 因子画布的文本入口。
 *
 * 为什么要有它：画布只能表达 DAG 画得出来的结构，而实际研究里更常见的是
 * 「贴一段公式 / 改一个窗口 / 快速试一版」。每次都从面板拖积木、连线、
 * 再点开 Inspector 改参数，成本远高于改一行文本。
 *
 * 这里**不做任何 DSL 解析**：服务端是唯一裁判。文本改动经 useFactorEditor
 * 防抖送 `POST /factors/ast`，结论（归一写法 / 报错）由 sync 回传；本组件
 * 只负责显示与上报输入。高亮纯粹为了好读，不承担校验职责 —— 前端自己
 * 造一套「看起来对」的语法，正是这个模块最该避免的漂移。
 */
import {
  Decoration,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  type DecorationSet,
  type ViewUpdate,
} from '@uiw/react-codemirror';
import dynamic from 'next/dynamic';
import { useMemo } from 'react';

import type { SyncState } from './useFactorEditor';

// CodeMirror 要量 DOM，必须关掉 SSR（与 backtests 的 EditorPane 同处理）
const CodeMirror = dynamic(() => import('@uiw/react-codemirror'), { ssr: false });

/** `$字段` */
const FIELD_RE = /\$[A-Za-z_][A-Za-z0-9_]*/g;
/** 算子名：后面紧跟 `(` 的标识符 */
const OP_RE = /\b[A-Za-z_][A-Za-z0-9_]*(?=\s*\()/g;
/** 数字字面量 */
const NUMBER_RE = /\b\d+(?:\.\d+)?\b/g;

/**
 * 用 MatchDecorator 做三色标记。
 *
 * 取色对齐 tailwind 设计 token：字段=靛（数据）、算子=墨加粗（结构）、
 * 数字=金（参数）。刻意不用涨跌红绿 —— 公式里的红绿会被误读成盈亏。
 */
function markPlugin(regexp: RegExp, className: string) {
  const decorator = new MatchDecorator({
    regexp,
    decoration: Decoration.mark({ class: className }),
  });
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = decorator.createDeco(view);
      }
      update(update: ViewUpdate) {
        this.decorations = decorator.updateDeco(update, this.decorations);
      }
    },
    { decorations: (view) => view.decorations },
  );
}

const HIGHLIGHT = [
  markPlugin(FIELD_RE, 'cm-dsl-field'),
  markPlugin(OP_RE, 'cm-dsl-op'),
  markPlugin(NUMBER_RE, 'cm-dsl-num'),
];

/** 浅色主题：与「研报台」纸面一致，不用 oneDark（那是回测页的深色代码台） */
const DSL_THEME = EditorView.theme({
  '&': { fontSize: '12.5px', backgroundColor: 'transparent', color: '#22252B' },
  '.cm-content': {
    fontFamily: '"SF Mono", "JetBrains Mono", Menlo, Consolas, monospace',
    padding: '6px 0',
  },
  '.cm-scroller': { lineHeight: '1.6' },
  '.cm-gutters': { backgroundColor: 'transparent', border: 'none', color: '#94989F' },
  '.cm-activeLine': { backgroundColor: 'rgba(34, 37, 43, 0.035)' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: '#5C6169' },
  '&.cm-focused': { outline: 'none' },
  '.cm-dsl-field': { color: '#31589E' },
  '.cm-dsl-op': { color: '#22252B', fontWeight: '600' },
  '.cm-dsl-num': { color: '#B08A3E' },
});

/** ⌘/Ctrl + ↵ 保存：研究时改一版就想存一版的路径要最短 */
function saveKeymap(onSave: () => void) {
  return EditorView.domEventHandlers({
    keydown: (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        onSave();
        return true;
      }
      return false;
    },
  });
}

export default function ExpressionPane({
  value,
  onChange,
  sync,
  onFormat,
  onRevert,
  onSave,
  collapsed = false,
  onToggle,
}: {
  value: string;
  onChange: (value: string) => void;
  sync: SyncState;
  onFormat: () => void;
  onRevert: () => void;
  onSave?: () => void;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const extensions = useMemo(
    () => [...HIGHLIGHT, DSL_THEME, EditorView.lineWrapping, ...(onSave ? [saveKeymap(onSave)] : [])],
    [onSave],
  );

  const trimmed = value.trim();
  /**
   * 结论是否属于当前这段文本。
   *
   * 少了这一步，用户刚敲完字、防抖还没触发时，上一次的「✓ 可解析」会继续
   * 挂在新文本上 —— 界面在替一段从未校验过的 DSL 背书。宁可显示「待校验」。
   */
  const stale = sync.expression !== trimmed;
  const phase: 'checking' | 'error' | 'ok' | 'idle' =
    sync.checking || (stale && trimmed.length > 0)
      ? 'checking'
      : sync.error
        ? 'error'
        : sync.normalized
          ? 'ok'
          : 'idle';

  // 同样要求结论属于当前文本：否则「归一为 X」说的是上一段文本的规范写法，
  // 与界面里正在显示的表达式对不上。
  const normalizedHint =
    !stale && sync.normalized && sync.normalized !== trimmed ? sync.normalized : null;

  return (
    <section className="flex shrink-0 flex-col border-b border-line bg-panel">
      <div className="flex items-center justify-between gap-3 px-3 py-1.5">
        <div className="flex min-w-0 items-center gap-2">
          {onToggle ? (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={!collapsed}
              title={collapsed ? '展开表达式直编' : '收起表达式直编'}
              className="shrink-0 rounded-[2px] px-1 text-ink-faint transition-colors hover:text-ink"
            >
              {collapsed ? '▸' : '▾'}
            </button>
          ) : null}
          <span className="shrink-0 text-[11px] tracking-[0.16em] text-ink-faint">
            DSL 表达式直编
          </span>
          <StatusText phase={phase} error={sync.error} />
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {onSave ? (
            <span className="mr-1 hidden text-[11px] text-ink-faint sm:inline">⌘/Ctrl + ↵ 保存</span>
          ) : null}
          <button
            type="button"
            className="btn btn-sm"
            onClick={onFormat}
            // 结论必须属于当前文本才允许归一：文本刚改、新结论还没回来时，
            // 手里的 normalized 是**上一段文本**的规范写法，点下去会把用户刚敲的
            // 改动整段抹掉。归一只是排版，不该有回退的破坏性。
            disabled={!sync.normalized || sync.normalized === value || stale}
            title={
              stale
                ? '等待这段文本的校验结果'
                : '用服务端归一后的规范写法替换当前文本'
            }
          >
            归一
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={onRevert}
            title="丢弃当前文本，回到画布上的结构"
          >
            回退到画布
          </button>
        </div>
      </div>

      {collapsed ? null : (
        <div className="border-t border-line">
          <CodeMirror
            value={value}
            height="132px"
            extensions={extensions}
            onChange={(next: string) => onChange(next)}
            placeholder="例如 Ts_Mean($close, 5) / $close —— 直接写 DSL，画布会跟着更新"
            basicSetup={{
              foldGutter: false,
              highlightActiveLineGutter: false,
              autocompletion: false,
              searchKeymap: false,
            }}
          />
        </div>
      )}

      {normalizedHint || sync.canvasWarnings.length > 0 || sync.translated ? (
        <div className="space-y-1 border-t border-line px-3 py-1.5">
          {normalizedHint ? (
            <p className="text-[11px] text-ink-faint">
              归一为 <code className="font-mono text-ink-dim">{normalizedHint}</code>
            </p>
          ) : null}
          {sync.translated ? (
            <p className="border-l-2 border-gold pl-2 text-[11px] text-gold">
              这段是历史 qlib 写法，已按统一引擎翻译成 lquant DSL；保存后以 DSL 存储。
            </p>
          ) : null}
          {sync.canvasWarnings.length > 0 ? (
            <ul className="space-y-0.5 border-l-2 border-gold pl-2 text-[11px] text-gold">
              {sync.canvasWarnings.map((w) => (
                <li key={w}>画布只能近似表示：{w}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/** 校验状态。文案只陈述服务端结论，不替它下判断。 */
function StatusText({ phase, error }: { phase: string; error: string | null }) {
  if (phase === 'checking') {
    return <span className="shrink-0 text-[11px] text-ink-faint">校验中…</span>;
  }
  if (phase === 'error') {
    return <span className="min-w-0 truncate text-[11px] text-up" title={error ?? ''}>✗ {error}</span>;
  }
  if (phase === 'ok') {
    return <span className="shrink-0 text-[11px] text-down">✓ 引擎可解析</span>;
  }
  return null;
}
