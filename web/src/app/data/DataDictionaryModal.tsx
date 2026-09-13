'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Empty, ErrorNote, Loading } from '@/components/States';
import { fetcher } from '@/lib/api';
import type { Dictionary } from './types';

/** 数据字典弹窗：GET /data/dictionary，按表分组渲染字段表。 */
export default function DataDictionaryModal({ onClose }: { onClose: () => void }) {
  const { data, error, isLoading } = useSWR<Dictionary>('/data/dictionary', fetcher);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col border border-line bg-paper shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-line px-4 py-2">
          <h3 className="font-song text-lg font-semibold">数据字典</h3>
          <button className="btn btn-sm" onClick={onClose} aria-label="关闭">✕</button>
        </header>
        <div className="flex-1 overflow-auto p-4">
          {error ? (
            <ErrorNote>加载失败：{String(error)}</ErrorNote>
          ) : isLoading ? (
            <Loading />
          ) : !data?.tables.length ? (
            <Empty>字典为空</Empty>
          ) : (
            <div className="space-y-5">
              {data.tables.map((t) => (
                <section key={t.table}>
                  <h4 className="mb-1 text-[13px] font-semibold text-ink">
                    <span className="font-mono">{t.table}</span>
                    <span className="ml-2 font-normal text-xs text-ink-faint">{t.description}</span>
                  </h4>
                  <table className="table-dense">
                    <thead>
                      <tr>
                        <th className="w-40 text-left">字段</th>
                        <th className="w-24 text-left">类型</th>
                        <th className="text-left">说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {t.fields.map((f) => (
                        <tr key={f.name}>
                          <td className="font-mono text-xs">{f.name}</td>
                          <td className="text-xs text-ink-faint">{f.type}</td>
                          <td className="text-xs text-ink-dim">{f.description}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
