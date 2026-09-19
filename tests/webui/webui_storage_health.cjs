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
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#storageHealthBreakdown > div").first().waitFor();
    const labels = await frame.locator("#storageHealthBreakdown span").allTextContents();
    assert.deepEqual(labels, ["画廊原图与参考图", "预览图", "ComfyUI 输入缓存", "ComfyUI 输出缓存", "ComfyUI 共享图片", "数据库", "升级备份", "其他临时文件", "配置及其他文件"]);
    assert.notEqual(await frame.locator("#storageHealthAllocated").textContent(), "-");
    assert.notEqual(await frame.locator("#storageHealthSize").textContent(), "-");
    assert.notEqual(await frame.locator("#storageHealthReusable").textContent(), "-");
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
    console.log(`${engine} ${width}: storage categories and unified maintenance passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"])) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
