import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import MlPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

afterEach(() => vi.unstubAllGlobals());

const status = {
  backends: ['lightgbm', 'ridge'],
  model_lines: 1,
  versions: 2,
  by_stage: { candidate: 1, staging: 0, production: 1, archived: 0 },
  production: { mom_line: { version: 2, metric: 0.041 } },
  signals: [{ name: 'mom_line', days: 21, last: '2026-09-30', version: 2 }],
  model_dir: 'data/models',
};

const models = [
  {
    name: 'mom_line', version: 1, run_id: 'r1', stage: 'archived',
    artifact_path: 'models/mom_line/v1/model.pkl', processor_path: null,
    metrics: { ml: { test_rank_ic_mean: 0.012 } }, fit_window: {},
    note: null, created_at: '2026-09-01T10:00:00', promoted_at: null,
  },
  {
    name: 'mom_line', version: 2, run_id: 'r2', stage: 'production',
    artifact_path: 'models/mom_line/v2/model.pkl', processor_path: 'models/mom_line/v2/processor.json',
    metrics: { ml: { test_rank_ic_mean: 0.041 } },
    fit_window: { train_end: '2026-06-30', test_end: '2026-09-30' },
    note: 'auto', created_at: '2026-09-02T10:00:00', promoted_at: '2026-09-02T10:05:00',
  },
];

const runs = [
  {
    run_id: 'r2', model: 'ridge', model_name: 'mom_line', model_version: 2,
    stage: 'production', metrics: { ml: { test_rank_ic_mean: 0.041 } },
    train_rows: 1200, test_rows: 300, train_end: '2026-06-30',
    test_end: '2026-09-30', created_at: '2026-09-02T10:00:00',
    artifact_path: 'models/mom_line/v2/model.pkl',
  },
];

describe('模型页', () => {
  it('渲染状态卡、版本表、训练记录与信号覆盖', async () => {
    stubPageFetch({
      '/ml/status': status,
      '/ml/models': models,
      '/ml/runs': runs,
    });
    renderPage(<MlPage />);

    // 状态卡
    await waitFor(() => expect(screen.getByText('lightgbm / ridge')).toBeInTheDocument());
    // 版本表：阶段徽章 + RankIC（「线上」在状态卡提示与下拉里也出现，用 getAll）
    expect(screen.getAllByText('线上').length).toBeGreaterThan(0);
    expect(screen.getAllByText('归档').length).toBeGreaterThan(0);
    expect(screen.getAllByText('v1').length).toBeGreaterThan(0);
    // 同一个 RankIC 在版本表与训练记录里各出现一次
    expect(screen.getAllByText('0.0410').length).toBe(2);
    // 训练记录：run_id 与 artifact
    expect(screen.getByText('r2')).toBeInTheDocument();
    // 信号覆盖
    expect(screen.getByText(/21 个交易日/)).toBeInTheDocument();
  });

  it('线上版本显示「回滚」，非线上版本显示「上线」', async () => {
    stubPageFetch({ '/ml/status': status, '/ml/models': models, '/ml/runs': runs });
    renderPage(<MlPage />);
    // v2 在版本表与信号提示里都出现 → 用 getAll
    await waitFor(() => expect(screen.getAllByText('v2').length).toBeGreaterThan(0));
    expect(screen.getByRole('button', { name: '回滚' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '上线' })).toBeInTheDocument();
  });

  it('点「上线」调 promote 接口并刷新列表', async () => {
    const fetchMock = stubPageFetch({
      '/ml/status': status, '/ml/models': models, '/ml/runs': runs,
    });
    renderPage(<MlPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: '上线' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '上线' }));
    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(
        ([u, init]) => String(u).includes('/ml/models/mom_line/1/promote')
          && (init as RequestInit | undefined)?.method === 'POST',
      );
      expect(posted).toBeTruthy();
    });
    await waitFor(() => expect(screen.getByText(/已上线/)).toBeInTheDocument());
  });

  it('接口失败时给出错误提示而不是空白', async () => {
    stubPageFetch({ '/ml/status': status, '/ml/runs': runs },
                  { '/ml/models': 500 });
    renderPage(<MlPage />);
    await waitFor(() => expect(screen.getByText(/模型列表加载失败/)).toBeInTheDocument());
  });

  it('空模型列表给出可执行下一步', async () => {
    stubPageFetch({ '/ml/status': { ...status, versions: 0, production: {}, signals: [] },
                    '/ml/models': [], '/ml/runs': [] });
    renderPage(<MlPage />);
    await waitFor(() => expect(screen.getByText(/还没有任何模型版本/)).toBeInTheDocument());
    expect(screen.getByText(/还没有训练记录/)).toBeInTheDocument();
    expect(screen.getByText(/还没有落库信号/)).toBeInTheDocument();
  });
});
