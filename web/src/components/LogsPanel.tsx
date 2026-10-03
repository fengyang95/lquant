'use client';

/**
 * 运行日志面板：应用日志级别过滤 + 关键词搜索，多行日志可展开全文。
 */
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { Panel } from '@/components/Panel';
import { Empty, ErrorNote } from '@/components/States';
import { fetcherData } from '@/lib/api';

type LogItem = {
  ts: string;
  level: string;
  run_id: string | null;
  source: string;
  message: string;
};

const LEVELS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部' },
  { value: 'DEBUG', label: 'DEBUG' },
  { value: 'INFO', label: 'INFO' },
  { value: 'WARNING', label: 'WARNING' },
  { value: 'ERROR', label: 'ERROR' },
];

export default function LogsPanel() {
  const [level, setLevel] = useState('');
  const [kw, setKw] = useState('');
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  // 搜索防抖 400ms，避免每个键击都打接口
  useEffect(() => {
    const t = setTimeout(() => setQuery(kw.trim()), 400);
    return () => clearTimeout(t);
  }, [kw]);

  // 不传 limit：后端返回读窗内全部匹配记录（ERROR 也一并给全，不再被 200 截断）
  const params = new URLSearchParams();
  if (level) params.set('level', level);
  if (query) params.set('q', query);
  const qs = params.toString();
  const key = `/monitor/app-logs${qs ? `?${qs}` : ''}`;
  const { data, error, isLoading } = useSWR<{ items: LogItem[]; total: number }>(key, fetcherData, {
    refreshInterval: 30000,
  });

  const items = data?.items ?? [];

  return (
    <Panel
      title="运行日志"
      meta={data ? `共 ${data.total} 条` : '加载中…'}
      actions={
        <>
          <div className="flex items-center gap-1">
            {LEVELS.map((lv) => (
              <button
                key={lv.value}
                className={`btn btn-sm ${level === lv.value ? 'btn-primary' : ''}`}
                onClick={() => setLevel(lv.value)}
              >
                {lv.label}
              </button>
            ))}
          </div>
          <input
            className="input h-7 w-40 text-xs"
            placeholder="搜索日志…"
            value={kw}
            onChange={(e) => setKw(e.target.value)}
          />
        </>
      }
    >
      {error ? (
        <ErrorNote>日志加载失败：{(error as Error).message}</ErrorNote>
      ) : isLoading ? (
        <div className="py-16 text-center text-sm text-ink-faint">日志加载中…</div>
      ) : items.length === 0 ? (
        <Empty>无匹配日志</Empty>
      ) : (
        <div className="max-h-96 overflow-auto">
          <table className="table-dense">
            <thead>
              <tr>
                <th>时间</th>
                <th>级别</th>
                <th>来源</th>
                <th>信息</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                // 稳定 key：30s 刷新会插入新记录，纯索引会展开态跳行
                const k = `${it.ts}-${it.source}-${it.message.slice(0, 48)}`;
                const open = expanded === k;
                return (
                  <tr
                    key={k}
                    className={open ? 'cursor-pointer bg-panel' : 'cursor-pointer'}
                    onClick={() => setExpanded(open ? null : k)}
                  >
                    {/* 带日期（原 slice(11) 只有 HH:MM:SS，跨天没法定位） */}
                    <td className="whitespace-nowrap tabular-nums">{it.ts.slice(0, 23)}</td>
                    <td
                      className={
                        it.level === 'ERROR' || it.level === 'CRITICAL'
                          ? 'text-up'
                          : it.level === 'WARNING'
                            ? 'text-gold'
                            : 'text-ink-dim'
                      }
                    >
                      {it.level}
                    </td>
                    <td className="font-mono text-xs">{it.source}</td>
                    <td className={open ? 'whitespace-pre-wrap' : 'truncate'}>
                      {it.message}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
