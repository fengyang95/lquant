// 回测工作台顶部工具条：新建/保存/校验/编译运行按钮与未保存标记，纯受控组件。
type RunBarProps = {
  dirty: boolean;
  busy: '' | 'save' | 'validate' | 'run';
  onNew(): void;
  onSave(): void;
  onValidate(): void;
  onRun(): void;
};

export default function RunBar({
  dirty,
  busy,
  onNew,
  onSave,
  onValidate,
  onRun,
}: RunBarProps) {
  const disabled = busy !== '';

  return (
    <div className="flex items-center gap-2">
      {dirty && <span className="text-xs text-gold">●未保存</span>}
      <button type="button" className="btn btn-sm" disabled={disabled} onClick={onNew}>
        新建
      </button>
      <button
        type="button"
        className="btn btn-sm btn-primary"
        disabled={disabled}
        onClick={onSave}
      >
        {busy === 'save' ? '保存中…' : '保存'}
      </button>
      <button
        type="button"
        className="btn btn-sm"
        disabled={disabled}
        onClick={onValidate}
      >
        {busy === 'validate' ? '校验中…' : '校验'}
      </button>
      <button
        type="button"
        className="btn btn-sm btn-accent"
        disabled={disabled}
        onClick={onRun}
      >
        {busy === 'run' ? '运行中…' : '编译运行 ▶'}
      </button>
    </div>
  );
}
