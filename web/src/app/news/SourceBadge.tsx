import type { NewsSourceKind } from './types';

/** 来源四色：telegraph=绿 / news=蓝 / social=紫 / report=橙（未知退灰） */
const KIND_BADGE: Record<NewsSourceKind, string> = {
  telegraph: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  news: 'bg-blue-50 text-blue-700 border-blue-200',
  social: 'bg-purple-50 text-purple-700 border-purple-200',
  report: 'bg-orange-50 text-orange-700 border-orange-200',
};

const KIND_TEXT: Record<NewsSourceKind, string> = {
  telegraph: '电报',
  news: '新闻',
  social: '社媒',
  report: '研报',
};

/** 来源类别徽章：色系 + 中文标签；未知 kind 原样透出退灰 */
export function SourceBadge({ kind }: { kind: string | null }) {
  const known = kind != null && kind in KIND_BADGE;
  const cls = known ? KIND_BADGE[kind as NewsSourceKind] : 'bg-neutral-100 text-neutral-500 border-neutral-200';
  const text = known ? KIND_TEXT[kind as NewsSourceKind] : (kind ?? '未知');
  return (
    <span
      className={`inline-block rounded-[2px] border px-1.5 py-0.5 text-xs whitespace-nowrap ${cls}`}
    >
      {text}
    </span>
  );
}

export default SourceBadge;
