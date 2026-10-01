'use client';

/**
 * 「存为新因子」—— 快速评价出结果后，把当前公式一键注册为因子。
 * 收起态只有一个按钮；展开后预填表达式，填名称（可加备注）后走 POST /factors。
 */
import { useState } from 'react';
import { post } from '@/lib/api';

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

export default function SaveAsFactor({
  formula, onSaved,
}: {
  formula: string;
  onSaved?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const nameValid = NAME_PATTERN.test(name);
  const canSubmit = Boolean(name) && nameValid && !busy;

  // 名称默认取公式派生（清洗非法字符），合法则预填，否则留空让用户命名
  function expand() {
    const derived = formula.replace(/[^A-Za-z0-9_]/g, '_').replace(/^_+|_+$/g, '');
    setName(NAME_PATTERN.test(derived) ? derived : '');
    setMsg('');
    setOpen(true);
  }

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setMsg('');
    try {
      await post('/factors', { name, expression: formula, description });
      setMsg(`✓ 已注册 ${name} → 因子库`);
      onSaved?.();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button type="button" onClick={expand} className="btn btn-sm">
        存为新因子
      </button>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="因子名（字母/下划线开头）"
        className="input input-mono w-44 py-1"
      />
      <input
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="备注（可选）"
        className="input w-44 py-1"
      />
      <input
        value={formula}
        readOnly
        placeholder="Rank(Ts_Mean($close,5)/$close-1)"
        title="将注册的表达式"
        className="input input-mono min-w-56 flex-1 py-1"
      />
      <button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        className="btn btn-primary btn-sm"
      >
        {busy ? '注册中…' : '注册'}
      </button>
      <button type="button" onClick={() => setOpen(false)} className="btn btn-sm">收起</button>
      {name && !nameValid && (
        <span className="text-red-600">因子名需以字母或下划线开头，仅含字母数字下划线</span>
      )}
      {msg && <span className={msg.startsWith('✗') ? 'text-red-600' : ''}>{msg}</span>}
    </div>
  );
}
