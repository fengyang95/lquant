'use client';

import { useState } from 'react';
import { post } from '@/lib/api';
import type { PurgeResult } from './types';

/** 数据清理/重刷弹窗：先预览（dry_run）再确认删除（POST /data/purge）。
 *  无过滤条件时后端 422，前端也做前置校验（至少一个过滤维度）。 */
export default function PurgeModal({
  onClose,
  onPurged,
}: {
  onClose: () => void;
  onPurged: (r: PurgeResult) => void;
}) {
  const [symbols, setSymbols] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [preview, setPreview] = useState<PurgeResult | null>(null);
  const [busy, setBusy] = useState<'' | 'preview' | 'purge'>('');
  const [err, setErr] = useState('');

  const params = () => ({
    dataset: 'daily',
    symbols: symbols.trim()
      ? symbols.split(/[,，\s]+/).filter(Boolean)
      : undefined,
    start: start || undefined,
    end: end || undefined,
    dry_run: true,
  });

  const hasFilter = !!(symbols.trim() || start || end);

  async function doPreview() {
    if (!hasFilter) {
      setErr('至少填写一个过滤条件（标的 / 起止日期），拒绝全量删除');
      return;
    }
    setBusy('preview');
    setErr('');
    try {
      const r = await post<PurgeResult>('/data/purge', params());
      setPreview(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }

  async function doPurge() {
    if (!preview) return;
    // 二次确认：删除不可恢复
    if (!window.confirm(`确认删除 ${preview.rows_matched.toLocaleString()} 行日线数据？删除后需重新回填。`)) return;
    setBusy('purge');
    setErr('');
    try {
      const r = await post<PurgeResult>('/data/purge', { ...params(), dry_run: false });
      onPurged(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy('');
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md border border-line bg-paper p-5 shadow-lg" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-1 font-song text-lg font-semibold">数据清理</h3>
        <p className="mb-4 text-xs text-ink-faint">
          按标的/日期范围删除日线湖数据（重刷前用）。先「预览」看将删行数，确认后不可恢复。
        </p>
        <label className="mb-3 block text-sm">
          <span className="mb-1 block text-xs text-ink-faint">标的（逗号分隔，留空 = 全部）</span>
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            placeholder="000001.SZ, 600000.SH"
            className="input input-mono w-full"
          />
        </label>
        <div className="mb-3 grid grid-cols-2 gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-faint">开始日期</span>
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="input input-mono w-full" />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-faint">结束日期</span>
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="input input-mono w-full" />
          </label>
        </div>

        {preview && (
          <div className="mb-3 border border-gold/40 bg-[#F7EFE6] px-3 py-2 text-sm">
            预览：将删除 <span className="font-song font-semibold text-up">{preview.rows_matched.toLocaleString()}</span> 行
            {preview.files?.length ? `（涉及 ${preview.files.length} 个 parquet 文件）` : ''}
          </div>
        )}
        {err && (
          <div className="mb-3 border-l-2 border-up bg-panel px-3 py-2 text-sm text-up">{err}</div>
        )}

        <div className="flex justify-end gap-2">
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn" onClick={doPreview} disabled={busy !== ''}>
            {busy === 'preview' ? '预览中…' : '预览'}
          </button>
          <button
            className="btn btn-primary text-up"
            onClick={doPurge}
            disabled={busy !== '' || !preview}
            title={!preview ? '先预览再删除' : undefined}
          >
            {busy === 'purge' ? '删除中…' : '确认删除'}
          </button>
        </div>
        {preview && (
          <p className="mt-2 text-right text-xs text-up">
            删除 {preview.rows_matched.toLocaleString()} 行后需重新回填才能恢复，请确认范围无误
          </p>
        )}
      </div>
    </div>
  );
}
