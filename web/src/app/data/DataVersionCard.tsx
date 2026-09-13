'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Empty } from '@/components/States';
import { fetcher } from '@/lib/api';
import type { DataVersion } from './types';

/** 页头数据版本小卡：最新 data_version + 可展开的版本历史列表。 */
export default function DataVersionCard() {
  const [open, setOpen] = useState(false);
  const { data: latest, error, isLoading } = useSWR<DataVersion>('/data/version/latest', fetcher, {
    // 404 / 空 = 湖为空，属正常态，不重试轰炸
    shouldRetryOnError: false,
  });
  const { data: versions } = useSWR<DataVersion[]>(
    open ? '/data/versions?limit=20' : null,
    fetcher,
  );

  if (isLoading) return null;
  if (error || !latest) return null;

  return (
    <div className="border border-line bg-panel px-4 py-2 text-sm">
      <button
        className="flex w-full items-center justify-between gap-3 text-left"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span>
          <span className="text-xs text-ink-faint">数据版本 · </span>
          <span className="font-medium">{latest.dataset}</span>
          <span className="ml-2 font-mono text-xs">{latest.version}</span>
        </span>
        <span className="text-xs text-ink-faint">
          {latest.created_at.slice(0, 19)}
          <span className="ml-2">{open ? '▾' : '▸'}</span>
        </span>
      </button>
      {open && (
        <div className="mt-2 border-t border-line pt-2">
          {!versions?.length ? (
            <Empty>暂无版本历史</Empty>
          ) : (
            <table className="table-dense">
              <thead>
                <tr>
                  <th className="text-left">数据集</th>
                  <th className="text-left">版本</th>
                  <th className="text-left">生成时间</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v, i) => (
                  <tr key={`${v.dataset}-${v.version}-${i}`}>
                    <td className="text-xs">{v.dataset}</td>
                    <td className="font-mono text-xs">{v.version}</td>
                    <td className="text-xs text-ink-faint">{v.created_at.slice(0, 19)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
