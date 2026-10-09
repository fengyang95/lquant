'use client';

/**
 * 行业详情页 —— 多角度分析报告（趋势 / 景气度 / 估值 / 资金拥挤度 / 宽度）。
 *
 * `identifier` 可以是行业代码（`801780.SI`）或中文名（`银行`）—— 后端两种都认，
 * 前端直接把它塞进路径，不再自己猜。
 */
import Link from 'next/link';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import PageHeader from '@/components/PageHeader';
import IndustryAnalysisView from '@/components/IndustryAnalysis';
import { ErrorNote, Loading } from '@/components/States';
import { ApiError, fetcher } from '@/lib/api';
import type { IndustryAnalysis } from '@/lib/industry';

export default function IndustryDetailPage() {
  const { identifier = '' } = useParams<{ identifier: string }>();
  const name = decodeURIComponent(identifier);

  const { data, error, isLoading } = useSWR<IndustryAnalysis>(
    name ? `/industry/${encodeURIComponent(name)}/analysis` : null,
    fetcher,
  );

  if (isLoading) return <Loading />;
  if (error) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="space-y-4">
        <PageHeader title={name} sub="行业分析" />
        <ErrorNote>
          {notFound ? `找不到行业「${name}」。` : `加载失败：${String(error)}`}
        </ErrorNote>
        <Link href="/industry" className="text-sm text-ink-dim hover:text-ink hover:underline">
          ← 返回行业轮动榜
        </Link>
      </div>
    );
  }
  if (!data) return <Loading />;

  const o = data.overview;
  return (
    <div className="space-y-5">
      <PageHeader
        title={data.industry}
        sub={
          <>
            <span className="font-mono">{data.industry_code}</span>
            {` · 观察日 ${data.asof} · 成分股 ${o.member_count} 只`}
            {o.rank
              ? ` · 全行业 20 日排名 ${o.rank.rank}/${o.rank.n_industries}`
                + `（${o.rank.percentile.toFixed(0)}% 分位）`
              : ''}
          </>
        }
        actions={
          <Link href="/industry" className="btn">
            返回榜单
          </Link>
        }
      />
      <IndustryAnalysisView report={data} />
    </div>
  );
}
