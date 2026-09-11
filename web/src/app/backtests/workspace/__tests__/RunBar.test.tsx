import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunBar from '../RunBar';

describe('RunBar', () => {
  it('渲染四个按钮并触发各回调', async () => {
    const onNew = vi.fn();
    const onSave = vi.fn();
    const onValidate = vi.fn();
    const onRun = vi.fn();
    render(
      <RunBar
        dirty={false}
        busy=""
        onNew={onNew}
        onSave={onSave}
        onValidate={onValidate}
        onRun={onRun}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: '新建' }));
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    await userEvent.click(screen.getByRole('button', { name: '校验' }));
    await userEvent.click(screen.getByRole('button', { name: /编译运行/ }));

    expect(onNew).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onValidate).toHaveBeenCalledTimes(1);
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('●未保存')).not.toBeInTheDocument();
  });

  it("busy='run' 时四按钮全部禁用且出现「运行中…」", () => {
    render(
      <RunBar
        dirty={false}
        busy="run"
        onNew={vi.fn()}
        onSave={vi.fn()}
        onValidate={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: '新建' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '校验' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '运行中…' })).toBeDisabled();
  });

  it("dirty=true 时显示「●未保存」", () => {
    render(
      <RunBar
        dirty
        busy=""
        onNew={vi.fn()}
        onSave={vi.fn()}
        onValidate={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText('●未保存')).toBeInTheDocument();
  });
});
