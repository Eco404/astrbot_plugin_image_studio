/* Real isolated gallery/settings APIs; external lifecycle is controlled here
 * so progress, interrupted scans and deletion failures are reproducible. */
const assert = require("node:assert/strict");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], calls = [];
  let enabled = false, status = "disabled", lastScan = 0, processed = 0;
  let externalId, secondId;
  const summary = () => ({ id: "nai", name: "NAI 插件图库", enabled, status, processed, total: 128, indexed_count: enabled ? 128 : 0, size_bytes: 2097152, thumbnail_count: 128, thumbnail_bytes: 1048576, last_scan_at: lastScan });
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => calls.push({ path: new URL(request.url()).pathname, body: request.method() === "POST" ? request.postDataJSON() : null }));
  await page.route("**/external/status", route => route.fulfill({ json: { sources: [summary()] } }));
  await page.route("**/external/scan", route => { assert.deepEqual(route.request().postDataJSON(), { source_id: "nai" }); status = "scanning"; processed = 37; return route.fulfill({ json: { scheduled: true } }); });
  await page.route("**/settings/get", async route => {
    const response = await route.fetch(); const payload = await response.json();
    payload.webui.external_sources = { nai: { enabled } };
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/settings/save", async route => {
    const body = route.request().postDataJSON(); enabled = body.studio.external_sources.nai.enabled;
    status = enabled ? "enumerating" : "disabled";
    // Keep the actual fixture scanner disabled: the external lifecycle in this
    // test is simulated, while ordinary settings persistence remains real.
    body.studio.external_sources.nai.enabled = false;
    const response = await route.fetch({ postData: body });
    await route.fulfill({ response });
  });
  await page.route("**/storage/health", async route => {
    const response = await route.fetch(); const payload = await response.json(); payload.external_sources = [summary()];
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/list?*", async route => {
    const response = await route.fetch(); const payload = await response.json();
    externalId ||= payload.items[0]?.id; secondId ||= payload.items[1]?.id;
    const item = payload.items.find(item => item.id === externalId);
    if (item) Object.assign(item, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, generation_engine: "novelai", source: "external" });
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/detail/**", async route => {
    const response = await route.fetch(); const payload = await response.json();
    if (payload.id === externalId) Object.assign(payload, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, source: "external" });
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/image-info/**", async route => {
    const response = await route.fetch(); const payload = await response.json();
    if (payload.detail_fields?.id === externalId) Object.assign(payload.detail_fields, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, source: "external" });
    await route.fulfill({ response, json: payload });
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    const inner = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.locator('[data-view="settings"]').click();
    const toggle = frame.locator('[data-external-enabled="nai"]');
    const statusLabel = frame.locator("[data-external-status]");
    await statusLabel.filter({ hasText: "已关闭" }).waitFor();
    assert.equal(await frame.locator("#externalSourcesPanel progress").count(), 0);
    assert.equal(await frame.locator("#storageExternalSources").isVisible(), false);
    await toggle.locator("..").click();
    await statusLabel.filter({ hasText: "保存全部设置后开始扫描" }).waitFor();
    await frame.locator("#saveSettingsButton").click();
    await statusLabel.filter({ hasText: "正在枚举历史文件" }).waitFor();
    await frame.locator("#storageExternalSources").filter({ hasText: "128 张" }).waitFor();
    status = "scanning"; processed = 37;
    await statusLabel.filter({ hasText: "正在扫描：37 张 / 128 张" }).waitFor();
    // A status refresh must not overwrite an unsaved switch value.
    await toggle.locator("..").click();
    status = "complete"; lastScan = Date.now() / 1000;
    await frame.locator("[data-external-last]").filter({ hasText: "最近扫描" }).waitFor();
    assert.equal(await toggle.isChecked(), false);
    assert.match(await statusLabel.textContent(), /保存全部设置后关闭扫描/);
    await toggle.locator("..").click();
    await statusLabel.filter({ hasText: "已完成：共 128 张" }).waitFor();
    await frame.locator("[data-external-scan]").click();
    await statusLabel.filter({ hasText: "正在扫描：37 张 / 128 张" }).waitFor();
    assert.match(await frame.locator("#storageExternalSources").innerText(), /外部原图[\s\S]*本地预览/);
    assert.ok(await inner.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));

    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-source-label.is-external").waitFor();
    const polls = calls.filter(call => call.path.endsWith("/external/status")).length;
    await page.waitForTimeout(1800);
    assert.equal(calls.filter(call => call.path.endsWith("/external/status")).length, polls, "external polling must stop off settings view");
    assert.equal(await frame.locator(".gallery-source-label.is-external").getAttribute("title"), "来自 NAI 插件图库");
    assert.equal(await frame.locator(".gallery-source-label.is-external").evaluate(element => getComputedStyle(element).outlineWidth), "1px");

    await frame.locator(`[data-gallery-id="${externalId}"] .gallery-info`).click();
    await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    await frame.locator("#drawerBody h3").filter({ hasText: "外部图片参数" }).waitFor();
    assert.equal(await frame.locator("#drawerBody h3").filter({ hasText: "原始请求" }).count(), 0);
    assert.equal(await frame.locator("#detailImportEdit").isVisible(), false);
    const favoriteMessage = await frame.locator("#detailFavorite").getAttribute("aria-pressed") === "true" ? "已取消收藏" : "已收藏";
    await frame.locator("#detailFavorite").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: favoriteMessage }).waitFor();
    assert.doesNotMatch(await frame.locator("#appNoticeMessage").textContent(), /保留保护/);
    await frame.locator("#detailDelete").click();
    await frame.locator(".external-delete-warning").filter({ hasText: "来源插件中的原图" }).waitFor();
    await frame.locator("#studioModalFooter button").first().click();

    const referenceRequest = page.waitForRequest(request => request.url().endsWith("/studio/reference/from-gallery"));
    await frame.locator("#detailUseReference").click();
    assert.ok((await referenceRequest).postDataJSON().image_id);
    await frame.locator(".reference-item").waitFor();
    assert.equal(calls.filter(call => call.path.endsWith("/studio/reference/upload")).length, 0, "gallery reuse copies originals server-side");

    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(`[data-select-id="${externalId}"]`).check();
    await frame.locator(`[data-select-id="${secondId}"]`).check();
    await page.route("**/gallery/delete/preview", route => {
      assert.deepEqual(new Set(route.request().postDataJSON().ids), new Set([externalId, secondId]));
      return route.fulfill({ json: { external_count: 1, external_sources: ["NAI 插件图库"] } });
    });
    await page.route("**/gallery/delete", route => {
      assert.equal(route.request().postDataJSON().confirm_external, true);
      return route.fulfill({ json: { deleted: [secondId], failed: [externalId], errors: [{ id: externalId, message: "原文件已被替换，请重新扫描" }] } });
    });
    await frame.locator("#deleteButton").click();
    await frame.locator("#confirmMessage").filter({ hasText: "永久删除来源插件中的原图" }).waitFor();
    await frame.locator("#confirmAccept").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "原文件已被替换" }).waitFor();
    assert.equal(await frame.locator(`[data-select-id="${externalId}"]`).isChecked(), true);
    assert.equal(await frame.locator(`[data-select-id="${secondId}"]`).isChecked(), false);
    assert.deepEqual(errors, []);
    console.log(`${name}: status, draft preservation, health, badge, external favorite/delete and original reference passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [engine, name, width] of [[chromium, "desktop", 1440], [chromium, "mobile", 390], [webkit, "webkit-mobile", 390]]) {
    const browser = await engine.launch({ headless: true });
    try { await verify(browser, name, width); } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
