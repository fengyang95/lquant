import { Empty } from '@/components/States';
import { SourceBadge } from './SourceBadge';
import type { NewsItemDTO } from './types';

/** published_at → 'MM-DD HH:mm'（非法/缺失 → —） */
export function fmtNewsTime(v: string | null): string {
  if (!v) return '—';
  const t = new Date(v).getTime();
  if (Number.isNaN(t)) return '—';
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 内容摘要：去空白，截 120 字 */
function excerpt(content: string | null): string {
  const s = (content ?? '').replace(/\s+/g, ' ').trim();
  return s.length > 120 ? `${s.slice(0, 120)}…` : s;
}

/**
 * 时间线资讯流：按 published_at 倒序展示（后端已排），空态 + 加载更多。
 * hasMore = offset + 已载条数 < total；onLoadMore 由父层负责翻页取数。
 */
export function NewsFeedList({
  items,
  total,
  limit,
  offset,
  onLoadMore,
}: {
  items: NewsItemDTO[];
  total: number;
  limit: number;
  offset: number;
  onLoadMore?: () => void;
}) {
  const loaded = offset + items.length;
  const hasMore = loaded < total;

  if (items.length === 0) {
    return <Empty>暂无资讯 —— 可在数据总览触发一次采集任务</Empty>;
  }

  return (
    <div>
      <ol className="divide-y divide-line">
        {items.map((n) => (
          <li key={n.news_id} className="flex gap-3 py-2.5">
            <span className="w-24 shrink-0 pt-0.5 font-mono text-xs tabular-nums text-ink-faint">
              {fmtNewsTime(n.published_at)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <SourceBadge kind={n.source_tag} />
                <span className="text-xs text-ink-faint">{n.source_name || n.source}</span>
                {n.title ? (
                  n.url ? (
                    <a
                      href={n.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-sm font-medium text-ink hover:underline"
                    >
                      {n.title}
                    </a>
                  ) : (
                    <span className="text-sm font-medium text-ink">{n.title}</span>
                  )
                ) : null}
              </div>
              {excerpt(n.content) ? (
                <p className="mt-0.5 text-xs leading-relaxed text-ink-faint">{excerpt(n.content)}</p>
              ) : null}
              {n.symbols && n.symbols.length > 0 ? (
                <div className="mt-1 flex flex-wrap gap-1">
                  {n.symbols.map((s) => (
                    <span key={s} className="rounded-[2px] bg-neutral-100 px-1 py-0.5 font-mono text-[11px] text-ink-faint">
                      {s}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      {hasMore && onLoadMore ? (
        <div className="border-t border-line px-4 py-3 text-center">
          <button
            onClick={onLoadMore}
            className="border border-line px-4 py-1.5 text-sm text-ink hover:bg-white disabled:opacity-40"
          >
            加载更多（已载 {loaded}/{total}）
          </button>
        </div>
      ) : null}
    </div>
  );
}

export default NewsFeedList;
