import { memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

/**
 * Markdown 渲染（问 AI 回答等）。走 react-markdown —— **默认不渲染原始 HTML**，
 * 内容里的 `<script>` 等只会被当文本转义，无 XSS 面；不引入 rehype-raw。
 *
 * 样式一律映射到「研报台」token（见 tailwind.config.ts）：禁用 tailwind 默认色，
 * 圆角 ≤ rounded-[2px]，无阴影。代码高亮的 `.hljs-*` 配色在 globals.css。
 *
 * 元素用 `{children}` 取内容而非展开 props：markdown 产出的属性里只有 `a.href` /
 * `code.className` 有意义，其余展开反而会把 hast 的 `node` 泄进 DOM。
 */
const components: Components = {
  h1: ({ children }) => (
    <h1 className="mb-2 mt-4 font-song text-lg text-ink first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 mt-4 font-song text-base text-ink first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mb-1.5 mt-3 font-song text-sm text-ink first:mt-0">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="mb-1 mt-3 font-song text-sm text-ink-dim first:mt-0">{children}</h4>
  ),
  h5: ({ children }) => (
    <h5 className="mb-1 mt-2 text-sm font-semibold text-ink-dim first:mt-0">{children}</h5>
  ),
  h6: ({ children }) => (
    <h6 className="mb-1 mt-2 text-xs font-semibold text-ink-faint first:mt-0">{children}</h6>
  ),
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="marker:text-ink-faint">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-line-strong pl-3 text-ink-dim">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-line" />,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-indigo underline underline-offset-2 hover:text-up"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  // 表格横向滚动：宽表不撑破 80% 宽的对话气泡
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="table-dense [&_td]:px-2 [&_th]:px-2 [&_th]:text-left">
        {children}
      </table>
    </div>
  ),
  // 只保留 className：rehype-highlight 把 `hljs language-*` 打在 code 上，丢了
  // 高亮配色（globals.css 的 .hljs-*）就落不上。行内/块级的**样式**交给 CSS
  // `.markdown-body :not(pre) > code` —— 靠 className 判断行内会把**无语言标记的
  // 裸围栏**误判成行内（rehype-highlight 默认不给它加类），套上描边+底色就难看。
  code: ({ className, children }) => <code className={className}>{children}</code>,
  pre: ({ children }) => (
    <pre className="my-3 overflow-x-auto rounded-[2px] border border-line bg-panel p-3 font-mono text-[12.5px] leading-relaxed">
      {children}
    </pre>
  ),
};

/** 回答随流式增量变化，父级 msgs 更新会让整列消息重渲 —— memo 让**没变的**那条
 *  跳过重新解析（react-markdown 每次都要重跑 remark/rehype 管线，不便宜）。 */
function Markdown({ content }: { content: string }) {
  return (
    <div className="markdown-body text-sm leading-relaxed text-ink">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default memo(Markdown);
