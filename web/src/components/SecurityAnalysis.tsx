'use client';

/**
 * 个股多角度分析报告渲染。
 *
 * 版式与行业分析共用 `ReportView`（同一份契约 → 同一套配色与结构），
 * 本组件只负责个股特有的空态措辞。
 */
import ReportView from '@/components/ReportView';
import { isSecurityAnalysis } from '@/lib/security';

export {
  AngleCard,
  MetricRow,
  StanceBadge,
} from '@/components/ReportView';

export default function SecurityAnalysisView({ report }: { report: unknown }) {
  // 先过个股契约校验（符号必须是字符串），再交给通用渲染器
  const valid = isSecurityAnalysis(report) ? report : null;
  return (
    <ReportView
      report={valid}
      emptyText="分析报告为空或结构不匹配 —— 请先同步该标的的数据"
    />
  );
}
