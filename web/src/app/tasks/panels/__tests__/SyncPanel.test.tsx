// SyncPanel 测试 —— 作业表渲染、立即运行/启停/删除请求、新建作业请求体、运行历史
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const useSWRMock = vi.fn();

vi.mock('swr', () => ({
  __esModule: true,
  default: (...args: unknown[]) => useSWRMock(...args),
}));

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();

vi.mock('@/lib/api', () => ({
  get: (...args: unknown[]) => getMock(...args),
  post: (...args: unknown[]) => postMock(...args),
  del: (...args: unknown[]) => delMock(...args),
}));

import SyncPanel from '../SyncPanel';

const jobs = [
  {
    sync_id: 'job-a',
    name: '行情采集',
    kind: 'collect',
    schedule_time: '15:05',
    weekdays: '1,2,3,4,5',
    params: { schedule: 'close' },
    enabled: true,
    last_run_at: '2024-06-01T15:05:00',
    last_status: 'ok',
    last_rows: 120,
  },
  {
    sync_id: 'job-b',
    name: '日线增量',
    kind: 'daily',
    schedule_time: '17:00',
    weekdays: '6,7',
    params: {},
    enabled: false,
    last_run_at: null,
    last_status: null,
    last_rows: null,
  },
];

const hist = [
  {
    run_id: 'run-1',
    sync_id: 'swan-collect',
    job_name: '行情采集',
    kind: 'collect',
    started_at: '2024-06-01T15:05:00',
    finished_at: '2024-06-01T15:06:00',
    rows: 120,
    status: 'ok',
    detail: { symbols: 5 },
  },
];

function setup() {
  useSWRMock.mockImplementation((key: string) => {
    if (key === '/sync/jobs') return { data: jobs, mutate: vi.fn() };
    if (key === '/sync/history?limit=30') return { data: hist, mutate: vi.fn() };
    return { data: undefined, mutate: vi.fn() };
  });
  render(<SyncPanel />);
}

describe('SyncPanel', () => {
  beforeEach(() => {
    useSWRMock.mockReset();
    useSWRMock.mockImplementation(() => ({ data: undefined, mutate: vi.fn() }));
    getMock.mockReset();
    postMock.mockReset();
    delMock.mockReset();
    postMock.mockResolvedValue({ status: 'ok', rows: 42 });
    delMock.mockResolvedValue({});
  });

  it('经 useSWR 轮询 /sync/jobs（10s）与 /sync/history（15s）', () => {
    setup();
    expect(useSWRMock).toHaveBeenCalledWith('/sync/jobs', expect.anything(), { refreshInterval: 10_000 });
    expect(useSWRMock).toHaveBeenCalledWith('/sync/history?limit=30', expect.anything(), { refreshInterval: 15_000 });
  });

  it('渲染作业表：类型/计划（wdText 工作日）/上次运行/状态标签', () => {
    setup();
    expect(screen.getAllByText('行情采集')).toHaveLength(2); // 作业表 + 历史表
    expect(screen.getAllByText('市场采集').length).toBeGreaterThan(0); // 作业表 + 表单 option
    expect(screen.getByText('工作日')).toBeInTheDocument();
    expect(screen.getByText('六·日')).toBeInTheDocument(); // wdText 非全周映射
    expect(screen.getByText('(close)')).toBeInTheDocument();
    expect(screen.getAllByText('06-01T15:05').length).toBeGreaterThan(0); // 注：未 replace('T',' ')，与 createdText 风格不一致
    expect(screen.getByText('120行')).toBeInTheDocument();
    expect(screen.getByText('ok', { selector: 'span' })).toBeInTheDocument();
    expect(screen.getByText('已停用')).toBeInTheDocument();
  });

  it('点「立即运行」→ POST /sync/run（demo）并透出反馈', async () => {
    setup();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: '立即运行' })[0]);
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/sync/run', { sync_id: 'job-a', demo: true });
    });
    expect(screen.getByText('✓ job-a 执行完成：ok，写入 42 行（demo 模式）')).toBeInTheDocument();
  });

  it('点「停用」→ POST toggle 携带 enabled 取反', async () => {
    setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '停用' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/sync/jobs/job-a/toggle', { enabled: false });
    });
  });

  it('点「启用」（已停用作业）→ POST toggle enabled=true', async () => {
    setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '启用' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/sync/jobs/job-b/toggle', { enabled: true });
    });
  });

  it('点「删除」→ del /sync/jobs/{id}', async () => {
    setup();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: '删除' })[0]);
    await waitFor(() => {
      expect(delMock).toHaveBeenCalledWith('/sync/jobs/job-a');
    });
  });

  it('新建 collect 作业 → POST /sync/jobs 携带 params.schedule，成功后清空 ID/名称', async () => {
    setup();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('ID'), 'swan-collect');
    await user.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/sync/jobs', {
        sync_id: 'swan-collect',
        name: 'swan-collect', // 名称空 → 回退 sync_id
        kind: 'collect',
        schedule_time: '15:05',
        weekdays: '1,2,3,4,5',
        params: { schedule: 'close' },
      });
    });
    expect(screen.getByText('✓ 已保存 swan-collect')).toBeInTheDocument();
    expect(screen.getByLabelText('ID')).toHaveValue('');
  });

  // 切换 kind 到 daily → params 传空对象且不渲染「采集时点」
  it('非 collect 类型 → params 传空对象', async () => {
    setup();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('类型'), 'daily');
    expect(screen.queryByLabelText('采集时点')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('ID'), 'daily-x');
    await user.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/sync/jobs', expect.objectContaining({ kind: 'daily', params: {} }));
    });
  });

  it('渲染运行历史表（状态 ok → 绿色，详情 JSON 截断）', () => {
    setup();
    expect(screen.getByText('120')).toBeInTheDocument();
    const cell = screen.getByText('ok', { selector: 'td' });
    expect(cell.className).toContain('text-down');
    expect(screen.getByText(/"symbols":5/)).toBeInTheDocument();
  });

  it('作业为空 → 空态文案', () => {
    useSWRMock.mockImplementation(() => ({ data: [], mutate: vi.fn() }));
    render(<SyncPanel />);
    expect(screen.getByText('还没有作业 —— 下方表单新建一个')).toBeInTheDocument();
  });

  it('历史为空 → 空态文案', () => {
    useSWRMock.mockImplementation((key: string) =>
      key === '/sync/jobs' ? { data: jobs, mutate: vi.fn() } : { data: [], mutate: vi.fn() },
    );
    render(<SyncPanel />);
    expect(screen.getByText('还没有运行记录 —— 点上面的「立即运行」试试')).toBeInTheDocument();
  });
});
