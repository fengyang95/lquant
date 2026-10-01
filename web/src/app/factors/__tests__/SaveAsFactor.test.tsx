import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SaveAsFactor from '../SaveAsFactor';

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));

vi.mock('@/lib/api', () => ({ get: vi.fn(), post: mockPost }));

describe('SaveAsFactor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPost.mockResolvedValue({ registered: 'myf' });
  });

  it('默认收起；展开后预填表达式，提交调 POST /factors 并回调', async () => {
    const onSaved = vi.fn();
    render(<SaveAsFactor formula="RSV10" onSaved={onSaved} />);
    expect(screen.queryByPlaceholderText(/^因子名/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /存为新因子/ }));
    const nameInput = screen.getByPlaceholderText(/^因子名/);
    expect((nameInput as HTMLInputElement).value).toBe('RSV10');
    expect((screen.getByPlaceholderText(/Rank\(Ts_Mean/) as HTMLInputElement).value).toBe('RSV10');

    fireEvent.change(nameInput, { target: { value: 'myf' } });
    fireEvent.click(screen.getByRole('button', { name: '注册' }));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/factors', {
        name: 'myf', expression: 'RSV10', description: '',
      });
    });
    expect(onSaved).toHaveBeenCalled();
    expect(await screen.findByText(/已注册 myf/)).toBeInTheDocument();
  });

  it('名称非法时提示且不发请求', async () => {
    render(<SaveAsFactor formula="RSV10" />);
    fireEvent.click(screen.getByRole('button', { name: /存为新因子/ }));
    fireEvent.change(screen.getByPlaceholderText(/^因子名/), { target: { value: '9x' } });
    fireEvent.click(screen.getByRole('button', { name: '注册' }));
    await vi.waitFor(() => {
      expect(screen.getByText(/因子名需以字母或下划线开头/)).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });
});
