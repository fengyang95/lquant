'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';

/**
 * 进入视口（提前 400px 预取）才真正挂载 children 的懒加载区块。
 * /data 页 9 个面板各自发请求，首屏全部并发拉起 → 首字节后 DOM 阻塞、
 * 后端瞬时 9 路请求。把折叠线以下的面板延迟到滚动可见时再挂载+请求，
 * 首屏只留 coverage + 采集健康度两组关键数据。
 * 未进入视口时渲染等高骨架（min-h 撑住布局，避免滚动跳动）。
 */
export default function LazySection({
  children,
  minHeight = 120,
}: {
  children: ReactNode;
  minHeight?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || visible) return;
    // 无 IntersectionObserver（老浏览器/SSR）→ 直接渲染，降级为旧行为
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          io.disconnect();
        }
      },
      { rootMargin: '400px 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visible]);

  if (visible) return <>{children}</>;
  return (
    <div ref={ref} style={{ minHeight }} aria-busy="true">
      <div className="h-full w-full animate-pulse rounded border border-line bg-panel/60" />
    </div>
  );
}
