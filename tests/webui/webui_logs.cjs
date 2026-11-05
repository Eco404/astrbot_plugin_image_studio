/* Log query, safe detail rendering, confirmation, pagination and stale loads. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const { pagePath } = require("../support/webui_paths.cjs");

async function checkDateLayout(page) {
  const dates = await page.locator('.logs-toolbar input[type="date"]').evaluateAll(inputs => inputs.map(input => ({
    input: input.getBoundingClientRect().toJSON(), field: input.parentElement.getBoundingClientRect().toJSON(),
  })));
  for (const { input, field } of dates) {
    assert.ok(input.width > 0 && input.left >= field.left - 1 && input.right <= field.right + 1, JSON.stringify({ input, field }));
  }
  if (Math.abs(dates[0].input.top - dates[1].input.top) < 1) {
    assert.ok(dates[0].input.right < dates[1].input.left, "date inputs must leave a gap instead of overlapping");
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
}

async function checkEntryCorners(page, expanded) {
  const corners = await page.locator("#logsList summary").first().evaluate(summary => {
    const style = getComputedStyle(summary);
    return [style.borderTopLeftRadius, style.borderTopRightRadius, style.borderBottomLeftRadius, style.borderBottomRightRadius].map(parseFloat);
  });
  assert.ok(corners.slice(0, 2).every(radius => radius > 0), "entry headers keep both outer top corners rounded");
  assert.ok(corners.slice(2).every(radius => expanded ? radius === 0 : radius > 0), `collapsed highlights must keep four rounded corners: ${corners}`);
}

async function verify(browser, width) {
  const mobile = width <= 600;
  const page = await browser.newPage({ viewport: { width, height: 850 }, isMobile: mobile, hasTouch: mobile });
  try {
    const index = fs.readFileSync(pagePath("index.html"), "utf8");
    const markup = index.match(/<section class="view" id="logsView">[\s\S]*?<\/section>\s*<\/section>/)[0].replace('class="view"', 'class="view is-active"');
    await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1"><main class="app-shell"><aside></aside><section class="workspace">${markup}</section></main>`);
    for (const link of index.matchAll(/href="\.\/([^"?]+\.css)/g)) await page.addStyleTag({ path: pagePath(link[1]) });
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
    await checkDateLayout(page);
    const summary = page.locator("#logsList summary");
    await summary.hover();
    await checkEntryCorners(page, false);
    await summary[mobile ? "tap" : "click"]();
    await page.locator("#logsList pre").waitFor();
    await checkEntryCorners(page, true);
    assert.match(await page.locator("#logsList pre").textContent(), /"prompt": "cat"/);
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await summary[mobile ? "tap" : "click"]();
    await checkEntryCorners(page, false);
    await page.evaluate(async () => {
      payload.items = Array.from({ length: 50 }, (_, index) => ({ ...payload.items[0], id: String(index), message: `日志 ${index + 1}` }));
      await controller.load();
    });
    const bar = page.locator(".logs-floatingbar");
    const before = await bar.boundingBox();
    assert.ok(before.y >= 750 && before.y + before.height <= 850, "pagination stays near the viewport bottom");
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const after = await bar.boundingBox();
    assert.ok(Math.abs(after.y - before.y) < 1, "pagination must remain fixed while the log list scrolls");
    const last = await page.locator("#logsList details").last().boundingBox();
    assert.ok(last.y + last.height <= after.y, "bottom spacing must keep the last log clear of the floating bar");
    await page.locator("#logsNext").click();
    await page.waitForFunction(() => calls.at(-1).params.page === 2);
    const label = page.locator("#logsPage"), picker = page.locator("#logsPagePicker");
    await label.click();
    assert.equal(await label.getAttribute("aria-expanded"), "true");
    assert.equal(await picker.isVisible(), true);
    assert.equal(await picker.locator('[aria-current="page"]').textContent(), "2");
    const pickerBox = await picker.boundingBox(), footerBox = await bar.boundingBox();
    assert.ok(pickerBox.y + pickerBox.height < footerBox.y && pickerBox.x >= 0 && pickerBox.x + pickerBox.width <= width, "page choices must fit above the unchanged footer");
    await picker.locator('[data-log-page="1"]')[mobile ? "tap" : "click"]();
    await page.waitForFunction(() => calls.at(-1).params.page === 1);
    assert.equal(await picker.isVisible(), false);
    assert.equal(await label.getAttribute("aria-expanded"), "false");
    await label.click(); await label.click();
    assert.equal(await picker.isVisible(), false);
    await label.click(); await label.press("Escape");
    assert.equal(await picker.isVisible(), false);
    assert.equal(await label.evaluate(element => element === document.activeElement), true);
    await label.click(); await page.locator(".section-heading h2").click();
    assert.equal(await picker.isVisible(), false, "outside clicks dismiss the picker");
    await label.click(); await page.evaluate(() => document.dispatchEvent(new Event("scroll")));
    assert.equal(await picker.isVisible(), false);
    if (mobile) {
      await label.click();
      await page.evaluate(() => {
        const view = document.getElementById("logsView");
        for (const [type, y] of [["touchstart", 100], ["touchmove", 120]]) {
          const event = new Event(type, { bubbles: true });
          Object.defineProperty(event, "touches", { value: [{ clientX: 100, clientY: y }] });
          view.dispatchEvent(event);
        }
      });
      assert.equal(await picker.isVisible(), false, "vertical touch scrolling dismisses page choices");
    }
    await page.evaluate(async () => { payload.total = 500000; await controller.load(); });
    await label.click();
    await page.locator("#logsPageCards").evaluate(strip => { strip.scrollLeft = strip.scrollWidth; });
    await picker.locator('[data-log-page="10000"]').waitFor();
    assert.ok(await picker.locator("[data-log-page]").count() < 50, "large logs must not render a button for every page");
    assert.equal(await picker.isVisible(), true, "horizontal page scrolling keeps choices open");
    await picker.locator('[data-log-page="10000"]')[mobile ? "tap" : "click"]();
    await page.waitForFunction(() => calls.at(-1).params.page === 10000);
    await label.click();
    assert.equal(await picker.locator('[aria-current="page"]').textContent(), "10000");
    await picker.locator('[data-log-page="10000"]').focus();
    await page.keyboard.press("Home");
    await page.waitForFunction(() => document.activeElement?.dataset.logPage === "1");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => calls.at(-1).params.page === 1);
    await label.click();
    assert.equal(await page.locator("#logsPageJump").isVisible(), width > 540);
    if (width > 540) {
      await page.locator("#logsPageInput").fill("37");
      await page.locator("#logsPageJump button").click();
      await page.waitForFunction(() => calls.at(-1).params.page === 37);
      await label.click();
    }
    await page.locator("#logsCategory").selectOption("request");
    await page.waitForFunction(() => calls.at(-1).params.category === "request" && calls.at(-1).params.page === 1);
    assert.equal(await picker.isVisible(), false, "filter changes dismiss stale page choices");
    await label.click();
    await page.evaluate(() => { delay = true; window.pending = controller.load(); });
    await page.waitForFunction(() => typeof release === "function");
    assert.equal(await label.isDisabled(), true);
    assert.equal(await picker.isVisible(), false, "refresh hides and disables stale page choices while loading");
    await page.evaluate(async () => { release(); await pending; delay = false; });
    await label.click();
    await page.evaluate(() => controller.setEnabled(false));
    assert.equal(await picker.isVisible(), false);
    assert.equal(await label.isDisabled(), true);
    await page.evaluate(async () => { controller.setEnabled(true); payload.total = 51; await controller.load(); });
    await page.locator("#logsLevel").selectOption("ERROR");
    await page.waitForFunction(() => calls.at(-1).params.level === "ERROR");
    await page.locator("#logsDateFrom").fill("2026-11-01");
    await page.locator("#logsDateFrom").dispatchEvent("change");
    await page.waitForFunction(() => calls.at(-1).params.date_from === "2026-11-01");
    await page.locator("#logsDateTo").fill("2026-11-05");
    await page.locator("#logsDateTo").dispatchEvent("change");
    await page.waitForFunction(() => calls.at(-1).params.date_to === "2026-11-05");
    await checkDateLayout(page);
    await page.locator("#logsClear").click();
    assert.equal(await page.evaluate(() => calls.some(call => call.path === "logs/clear")), false);
    await page.evaluate(() => { confirmed = true; });
    await page.locator("#logsClear").click();
    await page.waitForFunction(() => calls.some(call => call.path === "logs/clear" && call.body.confirm === true));
    await page.locator(".logs-empty").waitFor();
    assert.equal(await label.isDisabled(), true, "a single or empty page has no page choices");
    assert.equal(await picker.isVisible(), false);
    await page.evaluate(() => { delay = true; window.pending = controller.load(); });
    await page.waitForFunction(() => typeof release === "function");
    await page.evaluate(async () => { controller.setEnabled(false); payload = { enabled: true, total: 1, items: [{ message: "stale" }] }; release(); await pending; });
    assert.equal(await page.locator("#logsList").textContent(), "");
    assert.equal(await page.locator("#logsClear").isDisabled(), true);
    assert.equal(await page.locator("#logsEnabledNotice").isVisible(), true);
    assert.equal(await page.evaluate(() => calls.filter(call => call.path === "logs/clear").length), 1);
    console.log(`logs ${width}: responsive dates, hover/touch corners, floating page choices, large page counts, filters and disabled stale-response guard passed`);
  } finally { await page.close(); }
}

async function verifyIntegration(browser, width) {
  const base = process.env.STUDIO_TEST_URL;
  if (!base) return;
  const page = await browser.newPage({ viewport: { width, height: 900 }, isMobile: width <= 600, hasTouch: width <= 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(base);
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#logsNav").isVisible(), true);
    assert.equal(await frame.locator("#logsNav svg").count(), 1, "log navigation must use a bundled icon or explicit SVG fallback");
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card").first().waitFor();
    await frame.locator(".gallery-floatingbar").waitFor();
    const galleryBar = await frame.locator(".gallery-floatingbar").boundingBox();
    await page.route("**/logs?*", async route => {
      const response = await route.fetch();
      const body = await response.json(); body.total = 101;
      await route.fulfill({ response, json: body });
    });
    await frame.locator("#logsNav").click();
    assert.equal(await frame.locator("#logsCategory option").count(), 5);
    await frame.locator("#logsPage").filter({ hasText: "条" }).waitFor();
    const logsBar = await frame.locator(".logs-floatingbar").boundingBox();
    for (const key of ["x", "y", "width", "height"]) assert.ok(Math.abs(logsBar[key] - galleryBar[key]) < 1, `log pagination must align with gallery: ${key}`);
    await checkDateLayout(frame);
    await frame.locator("#logsPage").click();
    await frame.locator("#logsPagePicker").waitFor();
    const pickerArtifacts = path.resolve(__dirname, "../../dist/verification/logs-ui");
    fs.mkdirSync(pickerArtifacts, { recursive: true });
    await page.screenshot({ path: path.join(pickerArtifacts, `logs-pages-${browser.browserType().name()}-${width}.png`), animations: "disabled" });
    assert.equal(await frame.locator("#logsPagePicker").isVisible(), true);
    await frame.locator('[data-view="settings"]').click();
    assert.equal(await frame.locator(".logs-floatingbar").isVisible(), false, "the floating bar belongs only to the log page");
    assert.equal(await frame.locator("#logsPagePicker").evaluate(element => element.hidden), true, "leaving logs must reset the picker state");
    assert.equal(await frame.locator("#logsPage").getAttribute("aria-expanded"), "false");
    await page.unroute("**/logs?*");
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
    try {
      for (const width of [320, 390, 768, 1440]) await verify(browser, width);
      for (const width of [390, 1440]) await verifyIntegration(browser, width);
    }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
