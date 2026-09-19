/* Storage accounting and maintenance presentation against an isolated harness. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function verify(browser, engine, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    const categoryFixture = async route => {
      const response = await route.fetch();
      const payload = await response.json();
      const report = payload.data?.stats ? payload.data : payload;
      report.stats.disk.categories = {
        originals: { file_count: 1, file_bytes: (width < 600 ? 37 : 39) * 1024, allocated_bytes: (width < 600 ? 48 : 52) * 1024 },
        database: { file_count: 1, file_bytes: 20 * 1024, allocated_bytes: 24 * 1024 },
        comfy_inputs: { file_count: 1, file_bytes: 1024, allocated_bytes: 4096 },
        comfy_blobs: { file_count: 4, file_bytes: 4096, allocated_bytes: 16384 },
        ...(width < 600 ? { comfy_outputs: { file_count: 2, file_bytes: 2048, allocated_bytes: 4096 } } : {}),
      };
      report.stats.disk.file_bytes = 64 * 1024;
      report.stats.disk.file_count = width < 600 ? 9 : 7;
      report.stats.disk.allocated_bytes = 96 * 1024;
      report.stats.disk.database = { ...report.stats.disk.database, reusable_bytes: width < 600 ? 12 * 1024 : 0 };
      await route.fulfill({ response, json: payload });
    };
    await page.route("**/storage/health", categoryFixture);
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#storageHealthBreakdown > div").first().waitFor();
    const labels = await frame.locator("#storageHealthBreakdown span").allTextContents();
    assert.deepEqual(labels, ["画廊原图与参考图", "预览图", "ComfyUI 缓存", "数据库", "升级备份", "其他临时文件", "配置及其他文件"]);
    const cacheRow = frame.locator("#storageHealthBreakdown > div").filter({ has: frame.locator("span").filter({ hasText: /^ComfyUI 缓存$/ }) });
    assert.equal(await cacheRow.count(), 1);
    assert.equal(await cacheRow.locator("strong").textContent(), width < 600 ? "7 KB" : "5 KB", "cache total must include every available category once");
    assert.equal(await frame.locator("#storageHealthAllocated").textContent(), "96 KB");
    assert.equal(await frame.locator("#storageHealthSize").textContent(), "64 KB");
    assert.equal(await frame.locator("#storageHealthReusable").count(), 0);
    assert.equal(await frame.locator("#storageHealthGrid span").filter({ hasText: "数据库可复用空间" }).count(), 0);
    const databaseRow = frame.locator("#storageHealthBreakdown > div").filter({ has: frame.locator("span").filter({ hasText: /^数据库$/ }) });
    assert.equal(await databaseRow.locator("strong").textContent(), width < 600 ? "20 KB（12 KB 可压缩）" : "20 KB");
    await page.unroute("**/storage/health", categoryFixture);
    const maintenance = page.waitForResponse(response => response.url().endsWith("/storage/maintenance"));
    await frame.locator("#runMaintenanceButton").click();
    const response = await maintenance;
    assert.ok(response.ok(), await response.text());
    const report = await response.json();
    assert.equal(report.status, "healthy");
    assert.equal(report.database.reason, "manual_deep_only");
    assert.ok(report.stats.disk.categories.database.file_bytes > 0);
    assert.equal(typeof report.repaired.comfy_files, "number");
    await frame.locator("#runMaintenanceButton:not(:disabled)").waitFor();
    await frame.locator("#storageHealthStatus").filter({ hasText: "正常" }).waitFor();
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1), false);
    assert.deepEqual(errors, []);
    console.log(`${engine} ${width}: combined ComfyUI cache, preserved disk totals and unified maintenance passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"])) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
