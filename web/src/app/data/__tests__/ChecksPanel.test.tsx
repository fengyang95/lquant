import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SWRConfig } from 'swr';
import ChecksPanel from '../ChecksPanel';
import { checkText, checkTone, latestChecksByKind } from '../checks';

const run = (over: Record<string, unknown>) => ({
  run_id: 'r1', sync_id: 's', job_name: 'j', kind: 'daily', status: 'ok',
  started_at: '2026-09-18T15:05:00', finished_at: null, rows: 0, detail: null,
  ...over,
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('latestChecksByKind / checkTone / checkText', () => {
  it('每 kind 取最近一次带 checks 的运行', () => {
    const runs = [
      run({ kind: 'daily', detail: { checks: { coverage: { daily_missing_days: 0 } } } }),
      run({ kind: 'daily', detail: { checks: { coverage: { daily_missing_days: 2 } } } }),
      run({ kind: 'financial', detail: { checks: { checkpoint: { ok: true, covered_end: '2026-09-18', lag_trading_days: 0 } } } }),
      run({ kind: 'collect', detail: {} }),
    ];
    const s = latestChecksByKind(runs);
    expect(s).toHaveLength(2);
    expect(s[0].kind).toBe('daily');
    expect(s[0].tone).toBe('ok');
  });

  it('tone：检查不过 → warn，failed+skipped → bad/unknown', () => {
    expect(checkTone({ coverage: { daily_missing_days: 1 } }, 'partial')).toBe('warn');
    expect(checkTone({ coverage: { daily_missing_days: 0 } }, 'ok')).toBe('ok');
    expect(checkTone({ skipped: 'status=failed' }, 'failed')).toBe('unknown');
    expect(checkTone({}, 'ok')).toBe('unknown');
    expect(checkTone({ checkpoint: { ok: false, lag_trading_days: 5 } }, 'partial')).toBe('warn');
  });

  it('text：覆盖完整 / 缺失 / 断点滞后 / skipped 文案', () => {
    expect(checkText({ coverage: { daily_missing_days: 0 } })).toContain('覆盖完整');
    expect(checkText({ coverage: { daily_missing_days: 2 } })).toContain('缺失 2 个交易日');
    expect(checkText({ checkpoint: { ok: false, covered_end: '2026-09-18', lag_trading_days: 3 } }))
      .toContain('落后 3 个交易日');
    expect(checkText({ skipped: 'status=failed' })).toContain('未检查');
  });
});

describe('ChecksPanel 渲染', () => {
  function renderPanel(resp: unknown) {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/sync/history')) {
        return new Response(JSON.stringify(resp), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', f);
    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <ChecksPanel />
      </SWRConfig>,
    );
  }

  it('失败检查红色高亮，检查缺失显示未检查', async () => {
    renderPanel([
      run({ kind: 'daily', status: 'failed', detail: { checks: { skipped: 'status=failed' } } }),
      run({ kind: 'financial', status: 'failed', detail: { checks: { checkpoint: { ok: false, covered_end: '2026-09-18', lag_trading_days: 5 } } } }),
    ]);
    expect(await screen.findByText(/未检查（status=failed）/)).toBeTruthy();
    expect(screen.getByText(/落后 5 个交易日/)).toBeTruthy();
    const badRow = screen.getByText('财务数据').closest('li');
    expect(badRow?.className).toContain('border-up');
  });
});
