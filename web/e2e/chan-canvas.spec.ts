import { expect, test, type Page } from '@playwright/test';

/**
 * 缠论叠加层的**像素级**验证。
 *
 * 单测能验坐标换算与接线，但验不了「canvas 到底有没有画出东西」——
 * 而 lightweight-charts v4 没有自定义 series API，叠加层是手绘的，
 * 画不出来时页面不报错、只是图上什么都没有，属于最难发现的一类缺陷。
 *
 * 这里用路由打桩喂合成数据，不需要后端与数据湖；随后直接读叠加层的
 * `getImageData`，数非透明像素。
 */

const DAYS = 120;

/** 合成一段带波动的日线，保证 K 线有实体、笔有落差。 */
function makeBars() {
  const start = Date.UTC(2024, 0, 1);
  const bars = [];
  for (let i = 0; i < DAYS; i++) {
    const d = new Date(start + i * 86_400_000);
    const base = 20 + Math.sin(i / 9) * 4 + i * 0.02;
    const close = base + Math.sin(i / 3) * 0.4;
    const open = base;
    bars.push({
      trade_date: d.toISOString().slice(0, 10),
      open, close,
      high: Math.max(open, close) + 0.5,
      low: Math.min(open, close) - 0.5,
      volume: 100_000 + i * 100,
    });
  }
  return bars;
}

const BARS = makeBars();
const day = (i: number) => BARS[i].trade_date;

/** 与 BARS 对齐的缠论结构：两个中枢、若干笔、含买/卖/背驰各一路信号。 */
const CHAN = {
  symbol: '600519.SH', freq: '日线', available: true,
  asof: day(DAYS - 1), n_bars: DAYS,
  fx: [
    { dt: day(10), mark: 'D', value: BARS[10].low },
    { dt: day(30), mark: 'G', value: BARS[30].high },
    { dt: day(50), mark: 'D', value: BARS[50].low },
    { dt: day(70), mark: 'G', value: BARS[70].high },
  ],
  bi: [
    { sdt: day(10), edt: day(30), direction: 'up',
      high: BARS[30].high, low: BARS[10].low, n_bars: 12 },
    { sdt: day(30), edt: day(50), direction: 'down',
      high: BARS[30].high, low: BARS[50].low, n_bars: 11 },
    { sdt: day(50), edt: day(70), direction: 'up',
      high: BARS[70].high, low: BARS[50].low, n_bars: 13 },
  ],
  zs: [
    { sdt: day(30), edt: day(70), zg: BARS[70].high, zd: BARS[50].low,
      gg: BARS[70].high + 1, dd: BARS[50].low - 1, n_bis: 3 },
  ],
  signals: {
    latest: { buy1: '一买' },
    events: [
      { dt: day(50), kind: 'buy1', label: '一买', detail: '9笔' },
      { dt: day(70), kind: 'sell3', label: '三卖', detail: '' },
      { dt: day(70), kind: 'beichi', label: '背驰', detail: '7笔' },
    ],
  },
  notes: ['线段未实现：czsc 1.0.1 无线段（XD）实现。'],
};

const REGISTRY = {
  default: ['ma'],
  indicators: [{ name: 'ma', label: '均线族', category: 'trend', pane: 'price',
    min_window: 60, inputs: ['close'], outputs: ['ma5'] }],
};

async function stub(page: Page) {
  await page.route('**/api/data/indicators/registry', (r) =>
    r.fulfill({ json: REGISTRY }));
  await page.route('**/api/data/indicators?*', (r) =>
    r.fulfill({ json: BARS.map((b) => ({ ...b, ma5: b.close })) }));
  await page.route('**/api/data/quote*', (r) => r.fulfill({ json: {
    symbol: '600519.SH', name: '贵州茅台', available: true, price: 25,
    change_pct: 1.2, open: 24, high: 26, low: 23, prev_close: 24.7,
    amount: 1e8, market_cap: 1e11 } }));
  await page.route('**/api/security/*/chan*', (r) => r.fulfill({ json: CHAN }));
  await page.route('**/api/security/*/analysis', (r) => r.fulfill({ json: {} }));
  await page.route('**/api/fundamental/*', (r) =>
    r.fulfill({ json: { symbol: '600519.SH', available: false, hint: '', items: [] } }));
  await page.route('**/api/market/money-flow*', (r) => r.fulfill({ json: [] }));
}

/**
 * 叠加层（zIndex=10 的那块 canvas）的像素读数。
 *
 * 除非透明像素数外还给一个**像素签名哈希** —— 平移/缩放会挪动每一笔的位置，
 * 计数可能碰巧不变，哈希不会。
 */
function readOverlay(page: Page) {
  return page.evaluate(() => {
    const cv = [...document.querySelectorAll('canvas')].find(
      (c) => (c as HTMLCanvasElement).style.zIndex === '10') as HTMLCanvasElement | undefined;
    if (!cv) return { found: false, painted: 0, hash: 0 };
    const ctx = cv.getContext('2d');
    if (!ctx) return { found: true, painted: 0, hash: 0 };
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let painted = 0;
    let hash = 0;
    for (let i = 3; i < d.length; i += 4) {
      hash = (hash * 31 + d[i]) >>> 0;
      if (d[i] > 0) painted += 1;
    }
    return { found: true, painted, hash, w: cv.width, h: cv.height };
  });
}

test('缠论叠加层真的画出了东西（笔/中枢/分型/信号）', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await stub(page);

  await page.goto('/security/600519.SH', { waitUntil: 'domcontentloaded' });

  // 面板拿到数据（同时确认 URL 没写错 —— 打桩的是 /api/security/*/chan*）
  await expect(page.getByText('已确认笔数')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText('一买').first()).toBeVisible();

  // 叠加层存在且画了像素（轮询是因为重绘发生在 fitContent 之后）
  await expect.poll(async () => (await readOverlay(page)).painted,
    { timeout: 20_000, message: '缠论叠加层一个像素都没画' }).toBeGreaterThan(50);

  const painted = await readOverlay(page);
  expect(painted.found).toBe(true);
  expect(painted.w).toBeGreaterThan(0);
  expect(errors, `页面出现未捕获异常: ${errors.join('; ')}`).toEqual([]);
});

test('缩放后叠加层跟着重绘（不残留旧坐标）', async ({ page }) => {
  await stub(page);
  await page.goto('/security/600519.SH', { waitUntil: 'domcontentloaded' });
  await expect.poll(async () => (await readOverlay(page)).painted,
    { timeout: 20_000 }).toBeGreaterThan(50);

  const before = await readOverlay(page);
  // 在 K 线上滚轮缩放：必然改变可见区间，触发 subscribeVisibleLogicalRangeChange。
  // 比拖拽可靠 —— 拖拽是否被识别为平移取决于命中区域与手势阈值。
  //
  // **必须先 scrollIntoViewIfNeeded**：K 线在首屏之下，`boundingBox()` 给的是
  // 视口坐标，不滚动的话鼠标事件落在图表之外，什么都不会发生（连 K 线自己都不重绘），
  // 于是测试会误报成「叠加层不重绘」。
  const target = page.locator('canvas').first();
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  expect(box, 'K 线 canvas 应有可测尺寸').not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.wheel(0, -600);

  await expect.poll(async () => (await readOverlay(page)).hash,
    { timeout: 20_000, message: '缩放后叠加层没有重绘' }).not.toBe(before.hash);
});
