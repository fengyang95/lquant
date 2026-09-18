import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import MonitorPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

const summary = {
  procs: [
    { proc_name: 'collector', pid: 101, cpu_pct: 12.3, mem_rss_mb: 256, current_job: 'sync_daily', online: true, age_sec: 20 },
    { proc_name: 'worker-b', pid: null, cpu_pct: null, mem_rss_mb: null, current_job: null, online: false, age_sec: null },
  ],
  queues: [{ queue: 'default', pending: 3, failed: 1 }],
  api_live: { count: 42, p50: 10, p95: 88.8, err_rate: 0.01 },
  task_recent: [],
};

function setup() {
  return stubPageFetch({
    '/monitor/summary': summary,
    '/monitor/api-latency': { series: [], slowest: [] },
    '/monitor/tasks': { series: [] },
    '/monitor/data-pulls': { recent: [], by_job: [] },
    '/monitor/error-logs': { items: [], total: 0 },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('MonitorPage', () => {
  it('正常数据：渲染进程/队列/API 概览与进程状态表', async () => {
    setup();
    renderPage(<MonitorPage />);

    await waitFor(() => expect(screen.getAllByText('collector').length).toBeGreaterThan(0));
    expect(screen.getByText('监控')).toBeInTheDocument();
    expect(screen.getByText('1/2 在线')).toBeInTheDocument();
    expect(screen.getAllByText('worker-b').length).toBeGreaterThan(0);
    // 队列 + api_live
    expect(screen.getByText('default')).toBeInTheDocument();
    expect(screen.getByText('实时 API 请求数')).toBeInTheDocument();
    // 进程状态表状态列
    expect(screen.getByText('在线')).toBeInTheDocument();
    expect(screen.getByText('离线')).toBeInTheDocument();
  });

  it('fetch 失败：页面不崩溃，降级为空态文案', async () => {
    stubPageFetch({}, { '/monitor/summary': 500 });
    renderPage(<MonitorPage />);

    await waitFor(() => expect(screen.getByText('暂无进程样本')).toBeInTheDocument());
    expect(screen.getByText('监控')).toBeInTheDocument();
    expect(screen.getByText('暂无队列样本')).toBeInTheDocument();
  });
});
