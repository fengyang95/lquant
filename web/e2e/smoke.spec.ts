import { expect, test } from '@playwright/test';

/**
 * 冒烟测试：后端未启动时页面 fetch 会失败，组件应有错误态渲染。
 * 这里只断言页面壳/标题存在、无未捕获异常（白屏崩溃），不断言依赖真实数据的元素。
 */

const PAGES: { path: string; heading: RegExp }[] = [
  { path: '/', heading: /大盘|首页|仪表盘|LQuant|行情|概览/i },
  { path: '/data', heading: /数据/i },
  { path: '/tasks', heading: /任务/i },
  { path: '/monitor', heading: /监控/i },
  { path: '/news', heading: /新闻|资讯|快讯/i },
  { path: '/ask', heading: /问 AI|问答|提问|Ask|智能/i },
];

for (const { path, heading } of PAGES) {
  test(`页面 ${path} 正常加载且无未捕获异常`, async ({ page }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (err) => pageErrors.push(err.message));

    const response = await page.goto(path, { waitUntil: 'domcontentloaded' });
    expect(response?.status(), `GET ${path} 不应返回 5xx`).toBeLessThan(500);

    await expect(page.locator('body')).not.toBeEmpty();
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { level: 1 })).toContainText(heading);

    // 未捕获异常 = 组件在 fetch 失败时白屏崩溃，属缺陷
    expect(pageErrors, `页面 ${path} 出现未捕获异常: ${pageErrors.join('; ')}`).toEqual([]);
  });
}
