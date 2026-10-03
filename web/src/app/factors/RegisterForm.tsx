'use client';

/**
 * 因子注册 / 编辑表单（因子库）。
 * 字段：名称 + DSL 表达式 + 描述；算子面板点击插入；失焦实时 DSL 校验。
 * 编辑模式：editing 传入后表单回填，提交走 POST /factors（upsert）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { post } from '@/lib/api';
import { EMPTY_CATALOG, fetchCatalog } from '@/lib/factor-canvas/catalog';
import { snippetGroups } from '@/lib/factor-canvas/snippet';
import type { Catalog } from '@/lib/factor-canvas/types';

export type EditingFactor = {
  name: string;
  expression: string;
  description: string;
};

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

type SnippetItem = { key: string; label: string; insert: string; hint: string };

/** 算子按钮：点击把片段追加到表达式末尾 */
function OpButton({ item, onInsert }: { item: SnippetItem; onInsert: (s: string) => void }) {
  return (
    <button
      type="button"
      title={item.hint}
      onClick={() => onInsert(item.insert)}
      className="tag font-mono"
    >
      {item.label}
    </button>
  );
}

export default function RegisterForm({
  editing = null, onSaved,
}: {
  editing?: EditingFactor | null;
  onSaved?: () => void;
}) {
  const [name, setName] = useState('');
  const [expression, setExpression] = useState('');
  const [description, setDescription] = useState('');
  const [dslError, setDslError] = useState('' as string | null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [isEditing, setIsEditing] = useState(Boolean(editing));
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);
  const [catalogFailed, setCatalogFailed] = useState(false);
  const exprRef = useRef<HTMLInputElement>(null);

  // 算子清单来自服务端目录。旧版在这里硬编码了 Mean/Std/Corr/Ref/Delta/Ratio，
  // 其中 Delta/Ratio/Ref 引擎根本不认 —— 点出来的表达式过不了校验。
  useEffect(() => {
    let alive = true;
    fetchCatalog()
      .then((next) => {
        if (alive) setCatalog(next);
      })
      .catch(() => {
        // 目录拉不到就退化成纯文本输入，不挡注册流程 —— 但要说清楚，
        // 否则界面会永远停在"加载中…"
        if (alive) setCatalogFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const groups = useMemo(() => snippetGroups(catalog), [catalog]);

  // 编辑模式切换 → 回填并进入编辑；编辑清空（null）→ 退出编辑
  useEffect(() => {
    if (editing) {
      setName(editing.name);
      setExpression(editing.expression);
      setDescription(editing.description);
      setIsEditing(true);
    } else {
      setName('');
      setExpression('');
      setDescription('');
      setIsEditing(false);
    }
    setDslError(null);
    setMsg('');
  }, [editing]);

  const nameValid = NAME_PATTERN.test(name);

  const canSubmit = useMemo(
    () => Boolean(name) && nameValid && Boolean(expression.trim())
      && dslError === null && !busy,
    [name, nameValid, expression, dslError, busy],
  );

  // 失焦触发校验：编辑后 dslError 置 null（未校验状态），校验失败置错误信息
  async function validateNow() {
    const expr = expression.trim();
    if (!expr) { setDslError(null); return; }
    setChecking(true);
    try {
      const r = await post<{ ok: boolean; error: string | null }>('/factors/validate', { expression: expr });
      setDslError(r.ok ? null : (r.error ?? '校验失败'));
    } catch (e) {
      setDslError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }

  function insertOp(text: string) {
    setExpression((prev) => `${prev}${text}`);
    // 插入后旧校验结果失效
    setDslError(null);
    exprRef.current?.focus();
  }

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setMsg('');
    try {
      await post('/factors', { name, expression, description });
      setMsg(editing ? '✓ 已保存修改' : `✓ 已注册 ${name}`);
      onSaved?.();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  function cancelEdit() {
    setIsEditing(false);
    setName('');
    setExpression('');
    setDescription('');
    setDslError(null);
    setMsg('');
  }

  return (
    <div className="space-y-2">
      <div className="mb-1 flex items-baseline gap-2">
        <span className="text-[13px] font-semibold">
          {isEditing && editing ? `编辑因子：${editing.name}` : '注册新因子（DSL，AST 校验）'}
        </span>
        {checking && <span className="text-xs text-ink-faint">校验中…</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="因子名（字母/下划线开头）"
          disabled={isEditing}
          className="input w-48"
        />
        <input
          ref={exprRef}
          value={expression}
          onChange={(e) => { setExpression(e.target.value); setDslError(null); }}
          onBlur={validateNow}
          placeholder="Rank(Ts_Mean($close,5)/$close-1)"
          className="input input-mono min-w-72 flex-1"
        />
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="备注（可选）"
          className="input w-48"
        />
        {isEditing && (
          <button type="button" onClick={cancelEdit} className="btn btn-sm">
            取消
          </button>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit}
          className="btn btn-primary"
        >
          {busy ? '提交中…' : isEditing ? '保存修改' : '注册'}
        </button>
      </div>
      {name && !nameValid && (
        <p className="text-xs text-red-600">因子名需以字母或下划线开头，仅含字母数字下划线</p>
      )}
      {dslError !== null && dslError !== '' && (
        <p className="text-xs text-red-600">DSL 校验失败：{dslError}</p>
      )}
      {msg && <p className="text-xs text-ink-dim">{msg}</p>}
      <div className="pt-1">
        <div className="mb-1 text-xs text-ink-faint">点击插入（算子清单来自服务端目录）：</div>
        {groups.length === 0 ? (
          <span className="text-xs text-ink-faint">
            {catalogFailed ? '算子目录加载失败，可直接手写表达式' : '算子目录加载中…'}
          </span>
        ) : (
          groups.map((group) => (
            <div key={group.label} className="mb-1 flex flex-wrap items-center gap-1">
              <span className="w-20 shrink-0 text-[11px] text-ink-faint">{group.label}</span>
              {group.items.map((item) => (
                <OpButton key={item.key} item={item} onInsert={insertOp} />
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
