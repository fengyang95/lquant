import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import RegisterForm from '../RegisterForm';

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  get: mockGet,
  post: mockPost,
}));

function typeExpression(expr: string) {
  const input = screen.getByPlaceholderText(/Rank\(Ts_Mean/);
  fireEvent.change(input, { target: { value: expr } });
  return input;
}

describe('RegisterForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('提交合法因子：POST /factors 并回调成功', async () => {
    mockPost.mockResolvedValue({ registered: 'mom20' });
    mockGet.mockResolvedValue([]);
    const onSaved = vi.fn();
    render(<RegisterForm onSaved={onSaved} />);

    fireEvent.change(screen.getByPlaceholderText(/^因子名/), { target: { value: 'mom20' } });
    typeExpression('Rank($close/Mean($close,20)-1)');
    fireEvent.click(screen.getByRole('button', { name: '注册' }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/factors', {
        name: 'mom20', expression: 'Rank($close/Mean($close,20)-1)', description: '',
      });
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it('名称非法时本地拦截，不发起请求', async () => {
    render(<RegisterForm />);
    fireEvent.change(screen.getByPlaceholderText(/^因子名/), { target: { value: '9bad-name' } });
    typeExpression('close');
    fireEvent.click(screen.getByRole('button', { name: '注册' }));

    await vi.advanceTimersByTimeAsync(100);
    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.getByText(/因子名需以字母或下划线开头/)).toBeInTheDocument();
  });

  it('表达式失焦即调 validate 接口并展示错误', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/factors/validate') {
        return Promise.resolve({ ok: false, error: '未闭合括号' });
      }
      return Promise.resolve({});
    });
    render(<RegisterForm />);
    fireEvent.blur(typeExpression('1 +'));
    await screen.findByText(/未闭合括号/);

    // 校验失败时注册按钮禁用
    expect(screen.getByRole('button', { name: '注册' })).toBeDisabled();
  });

  it('算子面板由服务端目录驱动，点击插入的是完整合法片段', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/factors/ops') {
        return Promise.resolve({
          ops: [
            {
              name: 'Ts_Mean',
              category: 'TS',
              label: '时序均值',
              min_window: 1,
              series_arity: 1,
              params: [{ name: 'n', type: 'window', required: true, default: null }],
            },
          ],
          infix: [{ token: '/', label: '除法', arity: 2 }],
        });
      }
      if (path === '/factors/fields') {
        return Promise.resolve([{ name: 'close', label: '收盘价' }]);
      }
      return Promise.resolve([]);
    });

    render(<RegisterForm />);
    const input = screen.getByPlaceholderText(/Rank\(Ts_Mean/) as HTMLInputElement;

    // 目录异步到达后按钮才出现
    fireEvent.click(await screen.findByRole('button', { name: '时序均值' }));
    // 关键：插入的是带元数与窗口的完整调用，而不是旧版的半截 `Mean(`
    expect(input.value).toBe('Ts_Mean($close,5)');

    fireEvent.click(screen.getByRole('button', { name: '收盘价' }));
    expect(input.value).toBe('Ts_Mean($close,5)$close');

    // 旧版写死的引擎不认识的算子名不该再出现
    for (const stale of ['Delta', 'Ratio', 'Ref']) {
      expect(screen.queryByRole('button', { name: stale })).not.toBeInTheDocument();
    }
  });

  it('编辑模式回填并支持取消', async () => {
    mockPost.mockResolvedValue({});
    render(
      <RegisterForm
        editing={{ name: 'mom20', expression: 'close', description: '旧的' }}
        onSaved={vi.fn()}
      />,
    );
    expect((screen.getByPlaceholderText(/^因子名/) as HTMLInputElement).value).toBe('mom20');
    expect((screen.getByPlaceholderText(/Rank\(Ts_Mean/) as HTMLInputElement).value).toBe('close');
    expect((screen.getByPlaceholderText(/备注/) as HTMLInputElement).value).toBe('旧的');

    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/factors', {
        name: 'mom20', expression: 'close', description: '旧的',
      });
    });
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('button', { name: '保存修改' })).not.toBeInTheDocument();
  });
});
