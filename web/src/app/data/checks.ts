/** 完备性检查结果提取 —— 纯函数，供 ChecksPanel 与测试共用。
 *  数据来自 GET /sync/history 的 detail.checks（lquant.sync.manager._post_sync_check）。 */

export type CoverageCheck = {
  daily_missing_days?: number;
  repair_created?: boolean;
  issues_recorded?: number | null;
  error?: string;
};

export type CheckpointCheck = {
  ok?: boolean;
  covered_end?: string | null;
  lag_trading_days?: number | null;
  reason?: string | null;
  error?: string;
};

export type CheckBlock = {
  status?: string;
  coverage?: CoverageCheck;
  checkpoint?: CheckpointCheck;
  skipped?: unknown;
};

/** history 行中与检查相关的最小切片（detail 为后端任意 JSON） */
export type CheckRun = {
  job_name: string;
  kind: string;
  status: string;
  started_at: string | null;
  detail: Record<string, unknown> | null;
};

const KIND_LABEL: Record<string, string> = {
  daily: '日线增量',
  daily_basic: '基本面增量',
  financial: '财务数据',
  adj_factor: '复权因子',
  collect: '市场采集',
};

export type CheckSummary = {
  kind: string;
  label: string;
  run_status: string;
  started_at: string | null;
  tone: 'ok' | 'warn' | 'bad' | 'unknown';
  text: string;
};

/** 每个 kind 取最近一条带 checks 的运行记录（history 最新在前） */
export function latestChecksByKind(runs: CheckRun[]): CheckSummary[] {
  const seen = new Set<string>();
  const out: CheckSummary[] = [];
  for (const run of runs) {
    const checks = (run.detail ?? {}).checks as CheckBlock | undefined;
    if (!checks || typeof checks !== 'object') continue;
    if (seen.has(run.kind)) continue;
    seen.add(run.kind);
    out.push({
      kind: run.kind,
      label: KIND_LABEL[run.kind] ?? run.kind,
      run_status: run.status,
      started_at: run.started_at,
      tone: checkTone(checks, run.status),
      text: checkText(checks),
    });
  }
  return out;
}

/** 检查结论色调：skipped/检查缺失=unknown，检查不过=bad，partial=warn，其余 ok */
export function checkTone(checks: CheckBlock, runStatus: string): CheckSummary['tone'] {
  if (checks.skipped != null) return 'unknown';
  const hasCoverage = checks.coverage != null;
  const hasCheckpoint = checks.checkpoint != null;
  if (!hasCoverage && !hasCheckpoint) return 'unknown';
  if (hasCoverage && checks.coverage?.error) return 'warn';
  if (hasCheckpoint && checks.checkpoint?.error) return 'warn';
  if (runStatus === 'failed') return 'bad';
  if (hasCoverage && (checks.coverage?.daily_missing_days ?? 0) > 0) return 'warn';
  if (hasCheckpoint && !checks.checkpoint?.ok) return 'warn';
  return 'ok';
}

/** 人读的检查摘要文案 */
export function checkText(checks: CheckBlock): string {
  if (checks.skipped != null) return `未检查（${String(checks.skipped)}）`;
  const parts: string[] = [];
  const cov = checks.coverage;
  if (cov) {
    if (cov.error) parts.push(`覆盖度对账失败：${cov.error}`);
    else {
      const miss = cov.daily_missing_days ?? 0;
      parts.push(miss > 0 ? `缺失 ${miss} 个交易日` : '覆盖完整');
      if (cov.repair_created) parts.push('已建补齐任务');
      if (cov.issues_recorded != null) parts.push(`落库问题 ${cov.issues_recorded} 条`);
    }
  }
  const cp = checks.checkpoint;
  if (cp) {
    if (cp.error) parts.push(`断点对账失败：${cp.error}`);
    else if (!cp.ok) parts.push(`覆盖至 ${cp.covered_end ?? '—'}，落后 ${cp.lag_trading_days ?? '?'} 个交易日${cp.reason ? `（${cp.reason}）` : ''}`);
    else parts.push(`覆盖至 ${cp.covered_end ?? '—'}，无滞后`);
  }
  return parts.join(' · ') || '—';
}
