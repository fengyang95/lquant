'use client';

/**
 * 缠论（日线）结构面板。
 *
 * 与 K 线上的叠加层是**同一份数据、两种读法**：图上给「在哪」，这里给「是什么」。
 * 只展示**已确认**结构（后端无条件丢弃最新一笔），所以面板不会随最新一根 K 线抖动。
 */
import { Stat } from '@/components/Panel';
import { Empty, ErrorNote, Loading } from '@/components/States';
import {
  SIGNAL_LABELS,
  SIGNAL_ORDER,
  signalTone,
  type ChanData,
} from '@/lib/chan';

const MAX_EVENTS = 12;

function fmt(v: number | undefined | null): string {
  return v == null ? '—' : v.toFixed(2);
}

function toneClass(tone: 'up' | 'down' | 'neutral'): string {
  return tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-ink-dim';
}

export default function ChanPanel({ data, isLoading, error }: {
  data?: ChanData;
  isLoading?: boolean;
  error?: unknown;
}) {
  if (isLoading) return <Loading>缠论计算中…</Loading>;
  if (error) return <ErrorNote>缠论加载失败：{String(error)}</ErrorNote>;
  if (!data) return <Empty>暂无缠论数据</Empty>;
  if (!data.available) {
    return <Empty>{data.reason ?? '缠论不可用'}</Empty>;
  }

  const bi = data.bi ?? [];
  const zs = data.zs ?? [];
  const lastBi = bi[bi.length - 1];
  const lastZs = zs[zs.length - 1];
  const latest = data.signals?.latest ?? {};
  const events = [...(data.signals?.events ?? [])].reverse().slice(0, MAX_EVENTS);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-y-4 md:grid-cols-4">
        <Stat label="已确认笔数" value={bi.length} />
        <Stat label="中枢数" value={zs.length} />
        <Stat
          label="最近一笔"
          value={
            lastBi ? (
              <span className={lastBi.direction === 'up' ? 'text-up' : 'text-down'}>
                {lastBi.direction === 'up' ? '向上' : '向下'}
              </span>
            ) : '—'
          }
          hint={lastBi ? `${lastBi.sdt} → ${lastBi.edt}` : undefined}
        />
        <Stat
          label="当前中枢"
          value={lastZs ? `${fmt(lastZs.zd)} ~ ${fmt(lastZs.zg)}` : '—'}
          hint={lastZs ? `${lastZs.sdt} 起 · ${lastZs.n_bis} 笔` : undefined}
        />
      </div>

      {/* 六类信号的最新状态：无信号时留空槽位，避免布局跳动 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-ink-faint">当前信号</span>
        {SIGNAL_ORDER.map((k) => {
          const v = latest[k];
          return (
            <span
              key={k}
              className={`border px-2 py-0.5 text-xs ${
                v ? `border-line-strong ${toneClass(signalTone(k))}` : 'border-line text-ink-faint'
              }`}
            >
              {v ?? SIGNAL_LABELS[k]}
            </span>
          );
        })}
      </div>

      <div>
        <div className="mb-1 text-xs text-ink-faint">
          近期信号事件（沿变点，非逐日状态）
        </div>
        {events.length === 0 ? (
          <div className="text-xs text-ink-faint">窗口内无信号事件</div>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">日期</th>
                <th className="text-left">信号</th>
                <th className="text-left">明细</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={`${e.dt}-${e.kind}`} className="hover:bg-white">
                  <td className="tabular-nums">{e.dt}</td>
                  <td className={toneClass(signalTone(e.kind))}>{e.label}</td>
                  <td className="text-ink-dim">{e.detail || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(data.notes?.length ?? 0) > 0 && (
        <ul className="space-y-1 text-xs text-ink-faint">
          {data.notes!.map((n) => <li key={n}>· {n}</li>)}
        </ul>
      )}
    </div>
  );
}
