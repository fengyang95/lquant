'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote, Msg } from '@/components/States';
import { fetcher, putData } from '@/lib/api';
import {
  flagMonthlyCoverage, lastNMonthsRange, monthlyCoverageOption,
  type MonthlyCoverageResp,
} from '@/lib/coverage';
import Chart from '@/components/Chart';

const DEFAULT_THRESHOLD_PCT = 30;

/** 覆盖度按月缺口图：每月平均标的数柱状图，环比跌幅超阈值的月份标金。
 *  阈值读后端 settings（coverage_drop_warn_pct），响应缺 threshold 时回落 30。 */
export default function CoverageMonthlyChart() {
  const [start, end] = lastNMonthsRange(24);
  const { data, error, isLoading, mutate } = useSWR<MonthlyCoverageResp>(
    `/data/coverage/monthly?start=${start}&end=${end}`,
    fetcher,
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const thresholdPct = data?.threshold ?? DEFAULT_THRESHOLD_PCT;
  const rows = flagMonthlyCoverage(data?.rows ?? [], thresholdPct / 100);
  const option = rows.length ? monthlyCoverageOption(rows) : null;
  const flagged = rows.filter((r) => r.drop);

  async function editThreshold() {
    const raw = window.prompt(
      '环比跌幅告警阈值（%，5–95）',
      String(thresholdPct),
    );
    if (raw == null) return; // 取消
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 5 || n > 95) {
      setMsg('✗ 阈值需为 5–95 之间的数字');
      return;
    }
    setBusy(true);
    setMsg('');
    try {
      await putData('/settings/coverage_drop_warn_pct', {
        key: 'coverage_drop_warn_pct',
        value: String(n),
      });
      setMsg(`✓ 阈值已保存为 ${n}%，重新拉取覆盖度数据`);
      void mutate();
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="覆盖度 · 按月"
      meta={`近 24 个月 · 平均标的数/日${flagged.length ? ` · ${flagged.length} 个疑似缺口月` : ''}`}
      actions={
        <>
          <span className="text-xs text-ink-faint">
            阈值 {thresholdPct}% · 环比跌幅超过则标金
          </span>
          <button className="btn btn-sm" onClick={editThreshold} disabled={busy}>
            阈值设置
          </button>
          {flagged.length > 0 && (
            <span className="text-xs text-gold">
              <span className="mr-1 inline-block h-2 w-2 rounded-[2px] bg-gold align-middle" />
              疑似缺口
            </span>
          )}
        </>
      }
    >
      <Msg text={msg} />
      {error ? (
        <ErrorNote>加载失败：{String(error)}</ErrorNote>
      ) : isLoading ? (
        <p className="p-6 text-sm text-ink-faint">加载中…</p>
      ) : !rows.length ? (
        <Empty>暂无月度覆盖数据 —— 数据湖为空</Empty>
      ) : (
        <Chart option={option} height={220} />
      )}
    </Panel>
  );
}
