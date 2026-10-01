'use client';

/**
 * 因子注册 / 编辑表单（因子库）。
 * 字段：名称 + DSL 表达式 + 描述；算子面板点击插入；失焦实时 DSL 校验。
 * 编辑模式：editing 传入后表单回填，提交走 POST /factors（upsert）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { post } from '@/lib/api';

const OPERATORS: { label: string; insert: string; hint: string }[] = [
  { label: '$close', insert: '$close', hint: '收盘价' },
  { label: '$open', insert: '$open', hint: '开盘价' },
  { label: '$high', insert: '$high', hint: '最高价' },
  { label: '$low', insert: '$low', hint: '最低价' },
  { label: '$volume', insert: '$volume', hint: '成交量' },
  { label: 'Mean', insert: 'Mean(', hint: '时序均值' },
  { label: 'Rank', insert: 'Rank(', hint: '截面排名' },
  { label: 'Std', insert: 'Std(', hint: '时序标准差' },
  { label: 'Corr', insert: 'Corr(', hint: '相关系数' },
  { label: 'Ref', insert: 'Ref(', hint: '滞后取值' },
  { label: 'Delta', insert: 'Delta(', hint: '一阶差分' },
  { label: 'Ratio', insert: 'Ratio(', hint: '比值' },
];

export type EditingFactor = {
  name: string;
  expression: string;
  description: string;
};

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

/** 算子按钮：点击把 insert 追加到表达式末尾 */
function OpButton({ op, onInsert }: { op: typeof OPERATORS[number]; onInsert: (s: string) => void }) {
  return (
    <button
      type="button"
      title={op.hint}
      onClick={() => onInsert(op.insert)}
      className="tag font-mono"
    >
      {op.label}
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
  const exprRef = useRef<HTMLInputElement>(null);

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
      <div className="flex flex-wrap items-center gap-1 pt-1">
        <span className="text-xs text-ink-faint">点击插入：</span>
        {OPERATORS.map((op) => <OpButton key={op.label} op={op} onInsert={insertOp} />)}
      </div>
    </div>
  );
}
