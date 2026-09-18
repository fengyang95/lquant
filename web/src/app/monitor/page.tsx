'use client';

/**
 * 监控 —— 运维视角：进程/队列/API 耗时/任务延时/数据拉取。
 * summary 每 30s 自动刷新；曲线 on-mount + range 切换拉取。
 * 注意（T9 口径）：bucket 标签是「本地钟面值 + UTC(+00:00) 后缀」，展示时去后缀按本地钟面处理。
 */

import { useState } from 'react';
import useSWR from 'swr';
import Chart from '@/components/Chart';
import { Panel, Stat } from '@/components/Panel';
import PageHeader from '@/components/PageHeader';
import { Empty } from '@/components/States';
import LogsPanel from '@/components/LogsPanel';
import { fetcherData } from '@/lib/api';
import { C, axes, legend, tooltip } from '@/lib/chart';

type Proc = {
  proc_name: string; pid: number | null; cpu_pct: number | null; mem_rss_mb: number | null;
  current_job: string | null; online: boolean; age_sec: number | null;
};
type QueueRow = { queue: string; pending: number; failed: number };
type TaskRecent = Record<string, unknown>;
type Summary = {
  procs: Proc[]; queues: QueueRow[];
  api_live: { count: number; p50: number | null; p95: number | null; err_rate: number | null };
  task_recent: TaskRecent[];
};
type LatencyBucket = {
  bucket: string; count: number; avg: number | null; p50: number | null; p95: number | null; err_rate: number | null;
};
type ApiLatency = {
  series: LatencyBucket[];
  slowest: { route: string; count: number; avg: number | null; p95: number | null; err_rate: number | null }[];
};
type TaskBucket = { bucket: string; p95_delay: number | null; p95_elapsed: number | null; count: number };
type DataPull = {
  job: string; trade_date: string | null; started_at: string | null; finished_at: string | null;
  duration_ms: number | null; rows: number | null; status: string; message: string | null;
};
type DataPulls = {
  recent: DataPull[];
  by_job: { job: string; count: number; avg_duration_ms: number | null; failed: number }[];
};
type ErrorLog = {
  ts: string; route: string; method: string; status: number;
  error_type: string | null; message: string | null; traceback_tail: string | null;
};
type ErrorLogs = { items: ErrorLog[]; total: number };

const RANGES = ['1h', '6h', '24h', '7d'] as const;
type Range = (typeof RANGES)[number];

/** T9 口径：bucket 是「本地钟面 + UTC(+00:00) 后缀」，去后缀按本地钟面展示 */
function bucketLabel(b: string): string {
  return b.replace(/ ?UTC\(\+00:00\)$/, '');
}

function ageText(sec: number | null): string {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${Math.round(sec / 3600)}h`;
}

function num(v: number | null | undefined, digits = 1): string {
  return v == null ? '—' : v.toFixed(digits);
}

/** online 状态点：绿点在线 / 灰点离线 */
function OnlineDot({ online }: { online: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full align-middle ${online ? 'bg-down' : 'bg-ink-faint/50'}`}
      title={online ? '在线' : '离线'}
    />
  );
}

function RangeTabs({ range, onChange }: { range: Range; onChange: (r: Range) => void }) {
  return (
    <div className="flex gap-1">
      {RANGES.map((r) => (
        <button
          key={r}
          onClick={() => onChange(r)}
          className={`btn btn-sm ${range === r ? 'btn-primary' : ''}`}
        >
          {r}
        </button>
      ))}
    </div>
  );
}

export default function MonitorPage() {
  const [range, setRange] = useState<Range>('1h');
  const { data: sum } = useSWR<Summary>('/monitor/summary', fetcherData, { refreshInterval: 30_000 });
  const { data: apiLat } = useSWR<ApiLatency>(`/monitor/api-latency?range=${range}`, fetcherData);
  const { data: tasks } = useSWR<{ series: TaskBucket[] }>(`/monitor/tasks?range=${range}`, fetcherData);
  const { data: pulls } = useSWR<DataPulls>('/monitor/data-pulls', fetcherData, { refreshInterval: 60_000 });
  const { data: errLogs } = useSWR<ErrorLogs>(`/monitor/error-logs?range=${range}&limit=100`, fetcherData, { refreshInterval: 30_000 });
  const [expanded, setExpanded] = useState<string | null>(null);
  const errItems = errLogs?.items ?? [];
  const expandedRow = expanded
    ? errItems.find((e) => `${e.ts}-${e.route}-${e.message}` === expanded)
    : undefined;

  const procs = sum?.procs ?? [];
  const queues = sum?.queues ?? [];
  const live = sum?.api_live;

  // API 耗时曲线：p50/p95 双线（左轴 ms）+ 错误率（右轴 %）。双轴需对称 grid（左 52 右 52）保证对齐。
  const latSeries = apiLat?.series ?? [];
  const latencyOption = latSeries.length
    ? {
        tooltip: { ...tooltip, valueFormatter: (v: number) => (v == null ? '—' : `${v.toFixed(1)} ms`) },
        legend: legend({ top: 0 }),
        grid: { left: 52, right: 52, top: 30, bottom: 22 },
        ...axes({ data: latSeries.map((b) => bucketLabel(b.bucket)) }),
        yAxis: [
          { type: 'value', name: 'ms', axisLine: { show: false }, splitLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.inkDim, fontSize: 10 } },
          { type: 'value', name: '%', axisLine: { show: false }, splitLine: { show: false }, axisLabel: { color: C.inkDim, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(1)}%` } },
        ],
        series: [
          { name: 'p50', type: 'line', data: latSeries.map((b) => b.p50), showSymbol: false, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
          { name: 'p95', type: 'line', data: latSeries.map((b) => b.p95), showSymbol: false, lineStyle: { width: 1.5, color: C.up }, itemStyle: { color: C.up } },
          { name: '错误率', type: 'line', yAxisIndex: 1, data: latSeries.map((b) => b.err_rate), showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: C.gold }, itemStyle: { color: C.gold } },
        ],
      }
    : null;

  // 任务延时曲线：p95 queue_delay + p95 elapsed 双线（同一单位 ms）
  const taskSeries = tasks?.series ?? [];
  const taskOption = taskSeries.length
    ? {
        tooltip: { ...tooltip, valueFormatter: (v: number) => (v == null ? '—' : `${v.toFixed(0)} ms`) },
        legend: legend({ top: 0 }),
        grid: { left: 52, right: 16, top: 30, bottom: 22 },
        ...axes({ data: taskSeries.map((b) => bucketLabel(b.bucket)) }),
        series: [
          { name: 'p95 排队延时', type: 'line', data: taskSeries.map((b) => b.p95_delay), showSymbol: false, lineStyle: { width: 1.5, color: C.indigo }, itemStyle: { color: C.indigo } },
          { name: 'p95 执行耗时', type: 'line', data: taskSeries.map((b) => b.p95_elapsed), showSymbol: false, lineStyle: { width: 1.5, color: C.gold }, itemStyle: { color: C.gold } },
        ],
      }
    : null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="监控"
        sub="进程 / 队列 / API 耗时 / 任务延时 / 数据拉取 · summary 每 30s 刷新"
        actions={<RangeTabs range={range} onChange={setRange} />}
      />

      {/* stat 卡区：进程卡 + 队列 + api_live */}
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="进程" meta={`${procs.filter((p) => p.online).length}/${procs.length} 在线`}>
          {procs.length ? (
            <div className="flex flex-wrap gap-x-8 gap-y-4">
              {procs.map((p) => (
                <Stat
                  key={p.proc_name}
                  label={<><OnlineDot online={p.online} /> <span className="ml-1">{p.proc_name}</span></>}
                  value={<>{num(p.cpu_pct)}<span className="ml-1 text-xs text-ink-faint">%cpu</span></>}
                  hint={<><span className="font-mono">{num(p.mem_rss_mb, 0)}</span> MB · {ageText(p.age_sec)}</>}
                  tone={p.online ? 'text-ink' : 'text-ink-faint'}
                />
              ))}
            </div>
          ) : (
            <div className="py-4 text-xs text-ink-faint">暂无进程样本</div>
          )}
        </Panel>

        <Panel title="队列 / API">
          <div className="flex flex-wrap gap-x-10 gap-y-4">
            {queues.map((q) => (
              <Stat
                key={q.queue}
                label={q.queue}
                value={q.pending}
                tone={q.pending > 100 ? 'text-gold' : 'text-ink'}
                hint={<span className={q.failed > 0 ? 'text-up' : ''}>failed {q.failed}</span>}
              />
            ))}
            {queues.length === 0 && <div className="py-4 text-xs text-ink-faint">暂无队列样本</div>}
          </div>
          <div className="mt-4 flex flex-wrap gap-x-10 gap-y-4 border-t border-line pt-4">
            <Stat label="实时 API 请求数" value={live?.count ?? '—'} />
            <Stat label="API p95" value={live?.p95 != null ? num(live.p95) : '—'} hint="ms" />
            <Stat
              label="错误率"
              value={live?.err_rate != null ? `${(live.err_rate * 100).toFixed(2)}%` : '—'}
              tone={live?.err_rate != null && live.err_rate > 0.05 ? 'text-up' : 'text-ink'}
            />
          </div>
        </Panel>
      </div>

      {/* 进程状态表 */}
      <Panel title="进程状态" meta={`${procs.length} 个`}>
        {procs.length === 0 ? (
          <Empty>暂无进程样本 —— 确认 worker 已启动并上报心跳</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">进程</th>
                <th className="text-left">状态</th>
                <th className="text-right">CPU %</th>
                <th className="text-right">内存 MB</th>
                <th className="text-left">当前作业</th>
                <th className="text-right">样本年龄</th>
              </tr>
            </thead>
            <tbody>
              {procs.map((p) => (
                <tr key={p.proc_name} className="hover:bg-white">
                  <td>
                    <div className="font-medium">{p.proc_name}</div>
                    <div className="font-mono text-xs text-ink-faint">pid {p.pid ?? '—'}</div>
                  </td>
                  <td className="text-xs">
                    <OnlineDot online={p.online} />
                    <span className={`ml-1.5 ${p.online ? 'text-down' : 'text-ink-faint'}`}>
                      {p.online ? '在线' : '离线'}
                    </span>
                  </td>
                  <td className="text-right tabular-nums">{num(p.cpu_pct, 2)}</td>
                  <td className="text-right tabular-nums">{num(p.mem_rss_mb, 0)}</td>
                  <td className="text-xs font-mono">{p.current_job ?? '—'}</td>
                  <td className="text-right text-xs tabular-nums text-ink-dim">{ageText(p.age_sec)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {/* 曲线区 */}
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="API 耗时" meta={`range ${range}`}>
          {latencyOption ? (
            <Chart option={latencyOption} height={280} />
          ) : (
            <Empty>暂无耗时样本</Empty>
          )}
        </Panel>
        <Panel title="任务延时" meta={`range ${range}`}>
          {taskOption ? (
            <Chart option={taskOption} height={280} />
          ) : (
            <Empty>暂无任务样本</Empty>
          )}
        </Panel>
      </div>

      {/* 慢接口排行 */}
      <Panel title="慢接口 Top10" meta={`range ${range}`}>
        {!apiLat?.slowest?.length ? (
          <Empty>暂无数据</Empty>
        ) : (
          <table className="table-dense">
            <thead>
              <tr>
                <th className="text-left">路由</th>
                <th className="text-right">请求数</th>
                <th className="text-right">平均 ms</th>
                <th className="text-right">p95 ms</th>
                <th className="text-right">错误率</th>
              </tr>
            </thead>
            <tbody>
              {apiLat.slowest.map((r) => (
                <tr key={r.route} className="hover:bg-white">
                  <td className="font-mono text-xs">{r.route}</td>
                  <td className="text-right tabular-nums">{r.count}</td>
                  <td className="text-right tabular-nums">{num(r.avg)}</td>
                  <td className="text-right tabular-nums text-gold">{num(r.p95)}</td>
                  <td className={`text-right tabular-nums ${r.err_rate != null && r.err_rate > 0.05 ? 'text-up' : ''}`}>
                    {r.err_rate != null ? `${(r.err_rate * 100).toFixed(2)}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {/* 错误日志 */}
      <Panel title="错误日志" meta={`${errLogs?.total ?? 0} 条 · range ${range} · 每 30s 刷新`}>
        {!errLogs?.items?.length ? (
          <Empty>近端无错误 —— 服务运行正常</Empty>
        ) : (
          <div className="max-h-96 overflow-auto">
            <table className="table-dense">
              <thead className="sticky top-0 bg-panel">
                <tr>
                  <th className="text-left">时间</th>
                  <th className="text-left">路由</th>
                  <th className="text-right">状态</th>
                  <th className="text-left">错误类型</th>
                  <th className="text-left">信息</th>
                </tr>
              </thead>
              <tbody>
                {errLogs.items.map((e) => {
                  // key 不含下标：30s 自动刷新会插入新错误，下标位移会让展开态指错行
                  const key = `${e.ts}-${e.route}-${e.message}`;
                  const isOpen = expanded === key;
                  return (
                    <tr
                      key={key}
                      className={`cursor-pointer hover:bg-white ${isOpen ? 'bg-white' : ''}`}
                      onClick={() => setExpanded(isOpen ? null : key)}
                      title={e.traceback_tail ? '点击展开堆栈' : undefined}
                    >
                      <td className="text-xs tabular-nums">{e.ts?.slice(5, 19)}</td>
                      <td className="font-mono text-xs">{e.method} {e.route}</td>
                      <td className="text-right tabular-nums text-up">{e.status}</td>
                      <td className="text-xs font-medium text-up">{e.error_type ?? '—'}</td>
                      <td className="max-w-[24rem] truncate text-xs text-ink-dim" title={e.message ?? ''}>{e.message ?? '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {expandedRow?.traceback_tail && (
              <pre className="mt-2 max-h-56 overflow-auto rounded bg-ink-weak p-3 text-xs leading-5">{expandedRow.traceback_tail}</pre>
            )}
          </div>
        )}
      </Panel>

      {/* 数据拉取 */}
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="数据拉取 · 按作业聚合" meta={`${pulls?.by_job?.length ?? 0} 个作业`}>
          {!pulls?.by_job?.length ? (
            <Empty>暂无拉取记录</Empty>
          ) : (
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="text-left">作业</th>
                  <th className="text-right">次数</th>
                  <th className="text-right">平均耗时 ms</th>
                  <th className="text-right">失败</th>
                </tr>
              </thead>
              <tbody>
                {pulls.by_job.map((r) => (
                  <tr key={r.job} className="hover:bg-white">
                    <td className="font-mono text-xs">{r.job}</td>
                    <td className="text-right tabular-nums">{r.count}</td>
                    <td className="text-right tabular-nums">{num(r.avg_duration_ms, 0)}</td>
                    <td className={`text-right tabular-nums ${r.failed > 0 ? 'text-up' : 'text-ink-faint'}`}>{r.failed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="数据拉取 · 最近" meta={`${pulls?.recent?.length ?? 0} 条`}>
          {!pulls?.recent?.length ? (
            <Empty>暂无拉取记录</Empty>
          ) : (
            <div className="max-h-72 overflow-auto">
              <table className="table-dense">
                <thead className="sticky top-0 bg-panel">
                  <tr>
                    <th className="text-left">开始</th>
                    <th className="text-left">作业</th>
                    <th className="text-left">交易日</th>
                    <th className="text-right">耗时 ms</th>
                    <th className="text-right">行数</th>
                    <th className="text-left">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {pulls.recent.map((r, i) => (
                    <tr key={`${r.job}-${r.started_at ?? i}`} className="hover:bg-white">
                      <td className="text-xs tabular-nums">{r.started_at?.slice(5, 16) ?? '—'}</td>
                      <td className="text-xs font-mono">{r.job}</td>
                      <td className="text-xs tabular-nums">{r.trade_date ?? '—'}</td>
                      <td className="text-right text-xs tabular-nums">{r.duration_ms != null ? r.duration_ms.toLocaleString() : '—'}</td>
                      <td className="text-right text-xs tabular-nums">{r.rows != null ? r.rows.toLocaleString() : '—'}</td>
                      <td className={`text-xs font-medium ${
                        r.status === 'ok' ? 'text-down' : r.status === 'partial' ? 'text-gold' : 'text-up'}`}>
                        {r.status}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      {/* 运行日志：tail lquant.log，级别过滤 + 关键字搜索 */}
      <LogsPanel />
    </div>
  );
}
