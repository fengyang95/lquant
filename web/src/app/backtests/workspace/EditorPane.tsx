'use client';

/**
 * 工作台中间栏编辑器 + 参数区 —— 纯受控组件：内容全部来自 props，
 * 任何字段变化通过 onChange 上报全量快照。CodeMirror 动态加载（ssr:false）。
 */

import dynamic from 'next/dynamic';
import { python } from '@codemirror/lang-python';
import { oneDark } from '@codemirror/theme-one-dark';
import type { EditorParams, Snapshot } from './state';

const CodeMirror = dynamic(() => import('@uiw/react-codemirror'), { ssr: false });

type EditorPaneProps = {
  name: string;
  description: string;
  code: string;
  params: EditorParams;
  selectedId: string | null;
  onChange(next: Snapshot): void;
};

export default function EditorPane({
  name,
  description,
  code,
  params,
  selectedId,
  onChange,
}: EditorPaneProps) {
  function update(patch: Partial<Snapshot>) {
    onChange({ name, description, code, params: { ...params }, ...patch });
  }

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {selectedId !== null ? (
          <div className="input-mono" title="改名需另存为新策略">
            {name}
          </div>
        ) : (
          <label className="flex items-center gap-1 text-sm">
            策略名称
            <input
              className="input input-sm"
              aria-label="策略名称"
              value={name}
              onChange={(e) => update({ name: e.target.value })}
            />
          </label>
        )}
        <label className="flex items-center gap-1 text-sm">
          描述
          <input
            className="input input-sm"
            aria-label="描述"
            value={description}
            onChange={(e) => update({ description: e.target.value })}
          />
        </label>
        <label className="flex items-center gap-1 text-sm">
          开始日期
          <input
            className="input input-sm"
            aria-label="开始日期"
            type="date"
            value={params.start}
            onChange={(e) => update({ params: { ...params, start: e.target.value } })}
          />
        </label>
        <label className="flex items-center gap-1 text-sm">
          结束日期
          <input
            className="input input-sm"
            aria-label="结束日期"
            type="date"
            value={params.end}
            onChange={(e) => update({ params: { ...params, end: e.target.value } })}
          />
        </label>
        <label className="flex items-center gap-1 text-sm">
          因子公式
          <input
            className="input input-sm input-mono"
            aria-label="因子公式"
            value={params.formulas}
            placeholder="逗号分隔"
            onChange={(e) => update({ params: { ...params, formulas: e.target.value } })}
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <CodeMirror
          value={code}
          height="460px"
          extensions={[python()]}
          theme={oneDark}
          onChange={(v: string) => update({ code: v })}
        />
      </div>
    </div>
  );
}
