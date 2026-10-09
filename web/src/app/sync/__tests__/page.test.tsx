// /sync 数据操作页 —— 回归护栏：数据质量运维面板必须被真正挂载。
//
// 背景：PR #79 把 /data 拆成「概览页」、把操作类面板挪走后**忘了重新挂载**，
// CheckpointPanel / CrosscheckPanel / GapsPanel / PurgeModal / SourceConfigPanel
// 五个面板一度沦为「写了、测过、没人渲染」的孤岛（后端接口却一直在）。
// 本用例钉死「它们在 /sync 上渲染」这一契约，防止再次掉线。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SyncPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('/sync 数据操作页', () => {
  it('渲染五个数据质量运维面板 + 页头清理入口', async () => {
    stubPageFetch({
      // 注意：stubPageFetch 按声明序取首个子串命中，'/settings/providers' 必须排在 '/settings' 前
      '/settings/providers': [],
      '/settings': [],
      '/sync/jobs': [],
      '/sync/history': [],
      '/sync/freshness': { daily_lake: null, lag_days: null, news: null },
      '/data/crosscheck/issues': [],
      '/data/gaps': { window: { start: '2026-09-01', end: '2026-09-30' }, datasets: [] },
      '/data/checkpoints': [],
    });

    renderPage(<SyncPage />);

    // 五个面板的标题（懒加载区块在 effect 后挂载 → 用 findBy 异步等待）
    expect(await screen.findByText('跨源印证')).toBeInTheDocument();
    expect(await screen.findByText('数据缺口')).toBeInTheDocument();
    expect(await screen.findByText('断点续传')).toBeInTheDocument();
    expect(await screen.findByText('数据源配置')).toBeInTheDocument();
    // 同步作业本体仍在
    expect(screen.getByText('同步作业')).toBeInTheDocument();
    // 页头清理入口
    expect(screen.getByRole('button', { name: '数据清理' })).toBeInTheDocument();
  });

  it('点「数据清理」弹出 PurgeModal（走 POST /data/purge 的 dry_run 预览）', async () => {
    stubPageFetch({
      '/settings/providers': [],
      '/settings': [],
      '/sync/jobs': [],
      '/sync/history': [],
      '/sync/freshness': { daily_lake: null, lag_days: null, news: null },
      '/data/crosscheck/issues': [],
      '/data/gaps': { window: { start: '2026-09-01', end: '2026-09-30' }, datasets: [] },
      '/data/checkpoints': [],
    });

    renderPage(<SyncPage />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '数据清理' }));

    await waitFor(() => {
      expect(screen.getByText('确认删除')).toBeInTheDocument();
    });
    // 前置校验：未填任何过滤条件时点「预览」→ 拒绝全量删除
    await user.click(screen.getByRole('button', { name: '预览' }));
    expect(await screen.findByText(/拒绝全量删除/)).toBeInTheDocument();
  });
});
