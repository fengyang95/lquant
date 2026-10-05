import { describe, expect, it } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import FactorReportsPage from '../page';
import { renderPage, stubPageFetch } from '@/test/page-utils';

/** 一份当前口径报告 + 一份修复前的旧产物（无版本号）。 */
const reports = [
  {
    name: 'pct_change_20', url: '/api/factors/reports/pct_change_20', size_kb: 42,
    generator_version: '2.0', generated_at: '2026-10-05 11:00',
    current_version: '2.0', stale: false,
  },
  {
    name: 'BETA10', url: '/api/factors/reports/BETA10', size_kb: 40,
    generator_version: null, generated_at: '2026-09-20 09:00',
    current_version: '2.0', stale: true,
  },
  {
    name: 'syn_2f_eq', url: '/api/factors/reports/syn_2f_eq', size_kb: 38,
    generator_version: '2.0', generated_at: '2026-10-04 15:00',
    current_version: '2.0', stale: false,
  },
];

describe('因子报告中心', () => {
  it('按来源分组，并标出版本、生成时间与旧口径', async () => {
    stubPageFetch({ '/factors/reports': reports });
    renderPage(<FactorReportsPage />);

    await waitFor(() => expect(screen.getByText(/pct_change_20/)).toBeInTheDocument());
    // 分组标题
    expect(screen.getByText(/单因子评价/)).toBeInTheDocument();
    expect(screen.getByText(/合成因子/)).toBeInTheDocument();
    // 旧口径徽章 + 汇总告警（无版本号 = 修复前产物）
    expect(screen.getAllByText('旧口径').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/旧报告不会自动失效/)).toBeInTheDocument();
    // 版本与生成时间可见
    expect(screen.getAllByText('v2.0').length).toBe(2);
    expect(screen.getByText('2026-09-20 09:00')).toBeInTheDocument();
  });

  it('「只看当前口径」过滤掉旧报告', async () => {
    stubPageFetch({ '/factors/reports': reports });
    renderPage(<FactorReportsPage />);

    await waitFor(() => expect(screen.getByText(/BETA10/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/只看当前口径/));
    await waitFor(() => expect(screen.queryByText(/BETA10/)).not.toBeInTheDocument());
    expect(screen.getByText(/pct_change_20/)).toBeInTheDocument();
  });

  it('空列表给出下一步提示', async () => {
    stubPageFetch({ '/factors/reports': [] });
    renderPage(<FactorReportsPage />);
    await waitFor(() => expect(screen.getByText(/暂无报告/)).toBeInTheDocument());
  });
});
