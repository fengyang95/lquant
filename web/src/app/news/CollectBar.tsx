'use client';

import { useState } from 'react';
import { Msg } from '@/components/States';
import { FAST_SOURCES, triggerTask } from './lib';

/**
 * 资讯采集动作条：两种口径。
 * - 采集快讯：9 个单请求来源（电报/快讯/新闻/公告/榜单），十几秒内完成
 * - 全量采集：再加上逐股循环的个股新闻（em_news）与研报（em_research），
 *   同步请求可能数分钟，按钮在途禁用避免重复触发
 */
export function CollectBar({ onDone }: { onDone?: () => void }) {
  const [busy, setBusy] = useState<'' | 'fast' | 'all'>('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  const run = (mode: 'fast' | 'all') => {
    setBusy(mode);
    setNote('');
    setError('');
    const body = mode === 'fast' ? { sources: [...FAST_SOURCES] } : {};
    triggerTask(body)
      .then((r) => {
        setNote(`✓ 采集完成（${r.status}）：新增 ${r.rows_written ?? 0} 条`);
        onDone?.();
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(''));
  };

  return (
    <>
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy !== ''}
        title="9 个单请求来源（电报/快讯/新闻/公告/榜单），十几秒内完成"
        onClick={() => run('fast')}
      >
        {busy === 'fast' ? '采集中…' : '采集快讯'}
      </button>
      <button
        type="button"
        className="btn"
        disabled={busy !== ''}
        title="含逐股循环的个股新闻与研报，可能数分钟；同步请求请勿重复点击"
        onClick={() => run('all')}
      >
        {busy === 'all' ? '采集中…' : '全量采集'}
      </button>
      {error ? <span className="text-xs text-up">采集失败：{error}</span> : null}
      {note ? <Msg text={note} /> : null}
    </>
  );
}

export default CollectBar;
