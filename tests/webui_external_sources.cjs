/* Real isolated gallery/settings APIs; external lifecycle is controlled here
 * so progress, interrupted scans and deletion failures are reproducible. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-external-sources-"));

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], calls = [];
  let enabled = false, status = "disabled", lastScan = 0, processed = 0;
  let configured = { nai: { type: "nai", name: "NAI 插件图库", enabled, path: "", recursive: false, permissions: { favorite: true, delete: true, download: true, reference: true } } };
  let restricted = false;
  const allowed = () => ({ favorite: !restricted, delete: !restricted, download: !restricted, reference: !restricted });
  let externalId, secondId;
  const summary = () => ({ id: "nai", name: "NAI 插件图库", enabled, status, processed, total: 128, indexed_count: enabled ? 128 : 0, size_bytes: 2097152, thumbnail_count: 128, thumbnail_bytes: 1048576, last_scan_at: lastScan });
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => calls.push({ path: new URL(request.url()).pathname, body: request.method() === "POST" ? request.postDataJSON() : null }));
  await page.route("**/external/status", route => route.fulfill({ json: { types: [{ id: "nai", name: "NAI 插件图库", path: "/data/plugin_data/astrbot_plugin_nai_image/image_history" }, { id: "directory", name: "自定义目录" }], sources: Object.entries(configured).map(([id, value]) => id === "nai" ? summary() : { id, ...value, status: "complete", indexed_count: 4 }) } }));
  await page.route("**/external/scan", route => { assert.deepEqual(route.request().postDataJSON(), { source_id: "nai" }); status = "scanning"; processed = 37; return route.fulfill({ json: { scheduled: true } }); });
  await page.route("**/settings/get", async route => {
    const response = await route.fetch(); const payload = await response.json();
    payload.webui.external_sources = structuredClone(configured);
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/settings/save", async route => {
    const body = route.request().postDataJSON(); configured = structuredClone(body.studio.external_sources); enabled = !!configured.nai?.enabled;
    status = enabled ? "enumerating" : "disabled";
    // Keep the actual fixture scanner disabled: the external lifecycle in this
    // test is simulated, while ordinary settings persistence remains real.
    body.studio.external_sources = {};
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
    if (item) Object.assign(item, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, generation_engine: "novelai", source: "external", allowed_actions: allowed() });
    if (restricted) delete payload.revision;
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/image-sequence?*", async route => {
    const response = await route.fetch(); const payload = await response.json();
    for (const item of payload.items || []) if (item.generation_id === externalId) item.allowed_actions = allowed();
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/detail/**", async route => {
    const response = await route.fetch(); const payload = await response.json();
    if (payload.id === externalId) {
      Object.assign(payload, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, source: "external", allowed_actions: allowed(), time_source: "btime" });
      for (const image of payload.images || []) image.allowed_actions = allowed();
      if (restricted) Object.assign(payload, { prompt: "", model: "", parameters: {}, generation_engine: "unknown" });
    }
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/image-info/**", async route => {
    const response = await route.fetch(); const payload = await response.json();
    if (payload.detail_fields?.id === externalId) {
      Object.assign(payload.detail_fields, { is_external: true, external_source: { id: "nai", name: "NAI 插件图库" }, source: "external", allowed_actions: allowed(), time_source: "btime" });
      if (payload.image) payload.image.allowed_actions = allowed();
      if (restricted) { Object.assign(payload.detail_fields, { prompt: "", model: "", parameters: {}, generation_engine: "unknown" }); payload.image.metadata = { format: "unknown", normalized: {}, raw: {} }; }
    }
    await route.fulfill({ response, json: payload });
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    const inner = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.locator('[data-view="settings"]').click();
    const row = frame.locator('[data-external-source="nai"]');
    const statusLabel = row.locator("[data-external-status]");
    await statusLabel.filter({ hasText: "已停用" }).waitFor();
    assert.equal(await frame.locator("#externalSourcesPanel progress").count(), 0);
    assert.equal(await frame.locator("#storageExternalSources").isVisible(), false);
    await row.click();
    const toggle = frame.locator("#externalEditorEnabled");
    assert.equal(await frame.locator("#externalEditorPath").getAttribute("readonly"), "");
    assert.match(await frame.locator("#externalEditorPath").inputValue(), /image_history/);
    await toggle.locator("..").click();
    await frame.locator("#externalEditorApply").click();
    await statusLabel.filter({ hasText: "待保存" }).waitFor();
    assert.equal(calls.filter(call => call.path.endsWith("/external/scan")).length, 0);
    await frame.locator("#saveSettingsButton").click();
    await statusLabel.filter({ hasText: "扫描中" }).waitFor();
    await frame.locator("#storageExternalSources").filter({ hasText: "128 张" }).waitFor();
    status = "scanning"; processed = 37;
    await row.click();
    await frame.locator("#externalEditorStatus").filter({ hasText: "正在扫描：37 张 / 128 张" }).waitFor();
    // A status refresh must not overwrite an unsaved switch value.
    await toggle.locator("..").click();
    status = "complete"; lastScan = Date.now() / 1000;
    await frame.locator("#externalEditorName").fill("未确认的名称");
    await frame.locator("#externalEditorLast").filter({ hasText: "最近扫描" }).waitFor();
    assert.equal(await toggle.isChecked(), false);
    assert.equal(await frame.locator("#externalEditorName").inputValue(), "未确认的名称");
    assert.equal(await frame.locator("#externalEditorScan").isDisabled(), true);
    await frame.locator("#externalEditorName").fill("NAI 插件图库");
    await toggle.locator("..").click();
    await statusLabel.filter({ hasText: "正常" }).waitFor();
    await frame.locator("#externalEditorScan").click();
    await frame.locator("#externalEditorStatus").filter({ hasText: "正在扫描：37 张 / 128 张" }).waitFor();
    await frame.locator("#studioModalClose").click();
    assert.match(await frame.locator("#storageExternalSources").innerText(), /外部原图[\s\S]*本地预览/);
    assert.ok(await inner.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));

    await frame.locator("#addExternalSource").click();
    await frame.locator("#externalEditorType").selectOption("directory", { force: true });
    assert.equal(await frame.locator("#externalPermission-delete").isChecked(), false);
    assert.equal(await frame.locator("#externalPermission-favorite").isChecked(), true);
    await frame.locator("#externalEditorName").fill("普通图片");
    await frame.locator("#externalEditorPath").fill("/data/pictures");
    await page.screenshot({ path: path.join(output, `${name}-source-modal.png`), animations: "disabled" });
    await frame.locator("#externalEditorPath").fill("relative/path");
    await frame.locator("#externalEditorApply").click();
    await frame.locator("#studioModalError").filter({ hasText: "绝对路径" }).waitFor();
    await frame.locator("#externalEditorPath").fill("/data/pictures");
    await frame.locator("#externalEditorApply").click();
    const custom = frame.locator(".external-source-entry").filter({ hasText: "普通图片" });
    await custom.locator("[data-external-status]").filter({ hasText: "待保存" }).waitFor();
    await frame.locator("#saveSettingsButton").click();
    await custom.locator("[data-external-status]").filter({ hasText: "正常" }).waitFor();
    await frame.locator("#externalSourcesPanel").screenshot({ path: path.join(output, `${name}-source-list.png`), animations: "disabled" });
    await custom.click();
    await frame.locator("#externalEditorPath").fill("/data/pictures-2");
    await frame.locator("#externalEditorApply").click();
    await frame.locator("#studioModalTitle").filter({ hasText: "重建图库索引" }).waitFor();
    await frame.locator("#studioModalFooter button").filter({ hasText: "返回编辑" }).click();
    assert.equal(await frame.locator("#externalEditorPath").inputValue(), "/data/pictures-2");
    await frame.locator("#externalEditorRemove").click();
    await frame.locator("#studioModalTitle").filter({ hasText: "移除图库" }).waitFor();
    assert.match(await frame.locator("#studioModalBody").innerText(), /来源中的原文件.*保留/);
    await frame.locator("#studioModalFooter button").filter({ hasText: "确认移除" }).click();
    assert.equal(await custom.count(), 0);
    await page.waitForTimeout(1700);
    assert.equal(await custom.count(), 0, "status polling must not resurrect removed drafts");
    assert.equal(await frame.locator("#saveSettingsButton").evaluate(button => button.classList.contains("is-dirty")), true);

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
    await frame.locator(".external-delete-warning").filter({ hasText: "来源目录中的原图" }).waitFor();
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
    await frame.locator("#confirmMessage").filter({ hasText: "永久删除来源目录中的原图" }).waitFor();
    await frame.locator("#confirmAccept").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "原文件已被替换" }).waitFor();
    assert.equal(await frame.locator(`[data-select-id="${externalId}"]`).isChecked(), true);
    assert.equal(await frame.locator(`[data-select-id="${secondId}"]`).isChecked(), false);
    await page.route("**/gallery/delete/preview", route => route.fulfill({ json: { allowed: false, denied: [{ id: externalId, source_name: "NAI 插件图库", message: "NAI 插件图库未允许此操作" }], external_count: 1, external_sources: ["NAI 插件图库"] } }));
    const exportsBefore = calls.filter(call => call.path.endsWith("/gallery/export")).length;
    await frame.locator("#exportButton").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "未允许此操作" }).waitFor();
    assert.equal(calls.filter(call => call.path.endsWith("/gallery/export")).length, exportsBefore);
    const favoritesBefore = calls.filter(call => call.path.endsWith("/gallery/favorite")).length;
    await frame.locator("#favoriteSelectionButton:not(:disabled)").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "未允许此操作" }).waitFor();
    assert.equal(calls.filter(call => call.path.endsWith("/gallery/favorite")).length, favoritesBefore);
    await frame.locator("#cancelSelectionButton").click();
    restricted = true;
    await frame.locator("#galleryRefresh").click();
    await frame.locator(`[data-gallery-id="${externalId}"] .gallery-info`).click();
    await frame.locator("#drawerBody h3").filter({ hasText: "外部图片参数" }).waitFor();
    for (const id of ["detailFavorite", "detailDelete", "detailUseReference", "detailReproduce"]) assert.equal(await frame.locator("#" + id).isDisabled(), true, id);
    assert.match(await frame.locator("#detailDate").textContent(), /文件创建时间/);
    await frame.locator("[data-detail-image]").click();
    if (width < 600) {
      await frame.locator(".pswp--open").waitFor();
      await inner.waitForFunction(() => Array.from(document.querySelectorAll(".pswp__img")).some(image => image.naturalWidth > 1));
      assert.equal(await frame.locator(".pswp__button--image-studio-download").evaluate(button => button.hidden && getComputedStyle(button).display === "none"), true);
    } else {
      await frame.locator("#imagePreview:not(.is-hidden)").waitFor();
      assert.equal(await frame.locator("#downloadImageButton").isVisible(), false);
      assert.ok(await frame.locator("#previewImage").evaluate(image => image.naturalWidth > 1));
    }
    assert.deepEqual(errors, []);
    console.log(`${name}: source CRUD, draft/poll preservation, status/health, action permissions, badge and original reference passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [engine, name, width] of [[chromium, "desktop", 1440], [chromium, "mobile", 390], [webkit, "webkit-mobile", 390]]) {
    const browser = await engine.launch({ headless: true });
    try { await verify(browser, name, width); } finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
