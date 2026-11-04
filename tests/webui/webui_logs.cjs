/* Log query, safe detail rendering, confirmation, pagination and stale loads. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const { pagePath } = require("../support/webui_paths.cjs");

async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 850 } });
  try {
    await page.setContent(`<section id="logsView" class="logs-panel"><p id="logsEnabledNotice" hidden>日志未开启</p><div class="logs-toolbar"><label class="field">类型<select id="logsCategory"></select></label><label class="field">等级<select id="logsLevel"></select></label><label class="field">开始日期<input id="logsDateFrom" type="date"></label><label class="field">结束日期<input id="logsDateTo" type="date"></label><div class="logs-actions"><button id="logsRefresh">刷新</button><button id="logsClear">清空日志</button></div></div><p class="logs-hint">日期筛选按 UTC，显示时间采用浏览器时区。</p><div id="logsList"></div><div class="logs-pagination"><button id="logsPrevious">上一页</button><span id="logsPage"></span><button id="logsNext">下一页</button></div></section>`);
    await page.addStyleTag({ content: fs.readFileSync(pagePath("app.css"), "utf8") });
    await page.addStyleTag({ content: fs.readFileSync(pagePath("logs.css"), "utf8") });
    await page.addScriptTag({ path: pagePath("logs.js") });
    await page.evaluate(() => {
      window.calls = []; window.confirmed = false; window.delay = false;
      window.payload = { enabled: true, total: 51, items: [{ id: "one", category: "request", level: "DEBUG", timestamp: "2026-11-05T00:00:00Z", message: '<img src=x onerror="window.injected=true">', data: { method: "POST", url: "https://example.test/" + "a".repeat(300), body: { prompt: "cat", image: "[data URI omitted]" } } }] };
      window.controller = ImageStudioLogs.create({
        escape: value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
        apiGet: async (path, params) => { calls.push({ path, params }); if (delay) await new Promise(resolve => { window.release = resolve; }); return payload; },
        apiPost: async (path, body) => { calls.push({ path, body }); payload = { enabled: true, total: 0, items: [] }; return {}; },
        showNotice: () => {}, showConfirm: async () => confirmed,
      });
      controller.bind(); controller.bind();
    });
    await page.evaluate(() => controller.load());
    assert.equal(await page.locator("#logsList details").count(), 1);
    assert.equal(await page.locator("#logsList img").count(), 0);
    assert.equal(await page.locator("#logsList pre").count(), 0, "detail JSON is rendered only on demand");
    await page.locator("#logsList summary").click();
    await page.locator("#logsList pre").waitFor();
    assert.match(await page.locator("#logsList pre").textContent(), /"prompt": "cat"/);
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.locator("#logsNext").click();
    await page.waitForFunction(() => calls.at(-1).params.page === 2);
    await page.locator("#logsCategory").selectOption("request");
    await page.waitForFunction(() => calls.at(-1).params.category === "request" && calls.at(-1).params.page === 1);
    await page.locator("#logsLevel").selectOption("ERROR");
    await page.waitForFunction(() => calls.at(-1).params.level === "ERROR");
    await page.locator("#logsDateFrom").fill("2026-11-01");
    await page.locator("#logsDateFrom").dispatchEvent("change");
    await page.waitForFunction(() => calls.at(-1).params.date_from === "2026-11-01");
    await page.locator("#logsClear").click();
    assert.equal(await page.evaluate(() => calls.some(call => call.path === "logs/clear")), false);
    await page.evaluate(() => { confirmed = true; });
    await page.locator("#logsClear").click();
    await page.waitForFunction(() => calls.some(call => call.path === "logs/clear" && call.body.confirm === true));
    await page.locator(".logs-empty").waitFor();
    await page.evaluate(() => { delay = true; window.pending = controller.load(); });
    await page.waitForFunction(() => typeof release === "function");
    await page.evaluate(async () => { controller.setEnabled(false); payload = { enabled: true, total: 1, items: [{ message: "stale" }] }; release(); await pending; });
    assert.equal(await page.locator("#logsList").textContent(), "");
    assert.equal(await page.locator("#logsClear").isDisabled(), true);
    assert.equal(await page.locator("#logsEnabledNotice").isVisible(), true);
    assert.equal(await page.evaluate(() => calls.filter(call => call.path === "logs/clear").length), 1);
    console.log(`logs ${width}: filters, confirmation, pagination, safe/lazy details and disabled stale-response guard passed`);
  } finally { await page.close(); }
}

async function verifyIntegration(browser, width) {
  const base = process.env.STUDIO_TEST_URL;
  if (!base) return;
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(base);
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#logsNav").isVisible(), true);
    await frame.locator("#logsNav").click();
    assert.equal(await frame.locator("#logsCategory option").count(), 5);
    await frame.locator("#logsPage").filter({ hasText: "条" }).waitFor();
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#loggingEnabled").waitFor({ state: "attached" });
    await frame.waitForFunction(() => !document.getElementById("saveSettingsButton").disabled);
    const save = async () => {
      const pending = page.waitForResponse(response => response.url().endsWith("/settings/save") && response.request().method() === "POST");
      await frame.locator("#saveSettingsButton").click();
      assert.equal((await pending).status(), 200);
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    };
    await frame.locator("#loggingEnabled").locator("..").click();
    assert.equal(await frame.locator("#logsNav").isVisible(), true, "unsaved logging preference must not hide persisted entry");
    await save();
    await frame.locator("#logsNav").waitFor({ state: "hidden" });
    await page.waitForLoadState("networkidle");
    await frame.goto(frame.url());
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#logsNav").isVisible(), false, "disabled entry stays hidden after reload");
    await frame.locator('[data-view="settings"]').click();
    await frame.waitForFunction(() => !document.getElementById("saveSettingsButton").disabled);
    assert.equal(await frame.locator("#loggingEnabled").isChecked(), false);
    await frame.locator("#loggingEnabled").locator("..").click();
    await frame.locator("#loggingLevel").evaluate(input => { input.value = "DEBUG"; input.dispatchEvent(new Event("change", { bubbles: true })); });
    await frame.locator("#loggingRetentionDays").fill("0");
    await save();
    await frame.locator("#logsNav").waitFor();
    const gallery = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list`)).json();
    const edit = await page.request.post(`${base}/astrbot_plugin_image_studio/gallery/title`, { data: { generation_id: gallery.items[0].id, title: `日志界面测试 ${width}` } });
    assert.equal(edit.status(), 200);
    await frame.locator("#logsNav").click();
    await frame.locator("#logsList details").first().waitFor();
    await frame.locator("#logsCategory").evaluate(input => { input.value = "operation"; input.dispatchEvent(new Event("change", { bubbles: true })); });
    await frame.locator("#logsList[aria-busy='false']").waitFor();
    await frame.locator("#logsList summary").first().click();
    await frame.locator("#logsList pre").first().waitFor();
    await frame.evaluate(() => window.scrollTo(0, 0));
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const artifacts = path.resolve(__dirname, "../../dist/verification/logs-ui");
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, `logs-${width}.png`) });
    await frame.locator("#logsClear").click();
    await frame.locator("#confirmDialog:not(.is-hidden)").waitFor();
    await frame.locator("#confirmCancel").click();
    assert.ok(await frame.locator("#logsList details").count());
    await frame.locator("#logsClear").click();
    await frame.locator("#confirmAccept").click();
    await frame.locator(".logs-empty").waitFor();
    const payload = await (await page.request.get(`${base}/astrbot_plugin_image_studio/logs`)).json();
    assert.equal(payload.total, 0, "query and clear must not append their own operation events");
    const settings = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    assert.deepEqual(settings.webui.logging, { enabled: true, level: "DEBUG", retention_days: 0 });
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    assert.deepEqual(errors, []);
    console.log(`logs integrated ${width}: saved settings, hidden disabled navigation, reload, filters and confirmed clear passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"])) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) { await verify(browser, width); await verifyIntegration(browser, width); } }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
