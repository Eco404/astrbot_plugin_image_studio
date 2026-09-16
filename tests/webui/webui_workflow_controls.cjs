/* Run only against an isolated tests/support/webui_harness.py instance. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness URL.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-workflow-controls-"));
const plugin = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;

async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${plugin}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${method} ${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json();
  return body.data || body;
}

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})));
  });
}

async function geometry(frame, name) {
  const dimensions = await frame.evaluate(() => ({ viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth }));
  assert.ok(dimensions.page <= dimensions.viewport + 1, `${name}: horizontal overflow ${JSON.stringify(dimensions)}`);
}

async function capture(page, inner, filename) {
  const closeNotice = inner.locator("#appNoticeClose");
  if (await closeNotice.isVisible()) await closeNotice.click();
  await settle(inner); await geometry(inner, filename);
  await page.screenshot({ path: path.join(output, `${filename}.png`) });
}

function responseFor(page, pathPart, predicate = () => true) {
  return page.waitForResponse((response) => response.url().includes(pathPart) && response.request().method() !== "OPTIONS" && predicate(response));
}

async function selectValue(frame, inner, id, value) {
  if (await inner.locator(`#${id}`).evaluate(select => select.multiple)) {
    await frame.locator(`.studio-select-trigger[data-select-id="${id}"]`).click();
    if (value === "") await frame.locator('.studio-select-menu [data-select-action="all"]').click();
    else {
      await frame.locator('.studio-select-menu [data-select-action="clear"]').click();
      const index = await inner.locator(`#${id}`).evaluate((select, desired) => Array.from(select.options).findIndex(option => option.value === desired), value);
      assert.ok(index >= 0, `${id} lacks value ${value}`);
      await frame.locator(`.studio-select-menu [data-option-index="${index}"]`).click();
    }
    await frame.locator(`.studio-select-trigger[data-select-id="${id}"]`).press("Escape");
    return;
  }
  const index = await inner.locator(`#${id}`).evaluate((select, selected) => Array.from(select.options).findIndex((option) => option.value === selected), value);
  assert.ok(index >= 0, `${id} lacks value ${value}`);
  await frame.locator(`.studio-select-trigger[data-select-id="${id}"]`).click();
  await frame.locator(`.studio-select-menu[data-select-id="${id}"] [data-option-index="${index}"]`).click();
}

async function favoriteStatus(page, ids) {
  const result = await api(page, "post", "gallery/favorite/status", { generation_ids: ids });
  return result;
}

function favoritesFromStatus(result) {
  return Object.fromEntries((result.items || []).map((item) => [String(item.id || item.generation_id), !!item.is_favorite]));
}

async function testSelectionActions(page, frame, inner, name) {
  await frame.locator('[data-view="gallery"]').click();
  await frame.locator(".gallery-card").first().waitFor();
  assert.equal(await frame.locator("#galleryRetention").count(), 0, "obsolete near-limit gallery notice remains");
  const firstId = await frame.locator(".gallery-card").first().getAttribute("data-gallery-id");
  await api(page, "post", "gallery/favorite", { generation_id: firstId, favorite: true });
  await frame.locator(`.gallery-card[data-gallery-id="${firstId}"] .gallery-selection`).click();
  await frame.locator("#galleryNext").click();
  await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
  const secondId = await frame.locator(".gallery-card").first().getAttribute("data-gallery-id");
  assert.notEqual(firstId, secondId);
  await api(page, "post", "gallery/favorite", { generation_id: secondId, favorite: false });
  await frame.locator(`.gallery-card[data-gallery-id="${secondId}"] .gallery-selection`).click();
  await frame.locator("#selectionCount").filter({ hasText: "2" }).waitFor();

  const filterResponse = responseFor(page, "/gallery/list", (response) => new URL(response.url()).searchParams.get("sources") === '["command"]');
  await selectValue(frame, inner, "gallerySource", "command");
  await filterResponse;
  assert.match(await frame.locator("#selectionCount").textContent(), /2/, "filtering discarded cross-page selection");
  await frame.locator("#favoriteSelectionButton:not(:disabled)").waitFor();

  const addResponse = responseFor(page, "/gallery/favorite", (response) => response.request().method() === "POST" && !response.url().includes("/status") && response.request().postDataJSON()?.action === "toggle");
  await frame.locator("#favoriteSelectionButton").click();
  const addBodyRaw = await (await addResponse).json(); const addBody = addBodyRaw.data || addBodyRaw;
  assert.deepEqual(new Set(addBody.changed_ids), new Set([secondId]), "mixed selection must add only missing favorites");
  const added = favoritesFromStatus(await favoriteStatus(page, [firstId, secondId]));
  assert.equal(added[firstId], true); assert.equal(added[secondId], true);
  await frame.locator("#favoriteSelectionButton:not(:disabled)").waitFor();
  assert.match(await frame.locator("#selectionCount").textContent(), /2/);

  const removeResponse = responseFor(page, "/gallery/favorite", (response) => response.request().method() === "POST" && !response.url().includes("/status") && response.request().postDataJSON()?.action === "toggle");
  await frame.locator("#favoriteSelectionButton").click();
  const removeBodyRaw = await (await removeResponse).json(); const removeBody = removeBodyRaw.data || removeBodyRaw;
  assert.deepEqual(new Set(removeBody.changed_ids), new Set([firstId, secondId]), "all-favorite selection must unfavorite every selected record");
  const removed = favoritesFromStatus(await favoriteStatus(page, [firstId, secondId]));
  assert.equal(removed[firstId], false); assert.equal(removed[secondId], false);
  assert.match(await frame.locator("#selectionCount").textContent(), /2/);
  const buttonRows = await inner.locator("#selectionBar").evaluate((bar) => Array.from(bar.querySelectorAll("button")).filter((button) => button.getClientRects().length).map((button) => Math.round(button.getBoundingClientRect().top)));
  assert.ok(Math.max(...buttonRows) - Math.min(...buttonRows) < 2, `${name}: batch buttons wrapped into separate rows ${JSON.stringify(buttonRows)}`);
  await capture(page, inner, `${name}-batch-favorite`);

  const searchResponse = responseFor(page, "/gallery/list", (response) => new URL(response.url()).searchParams.get("query") === "构图 34");
  await frame.locator("#gallerySearch").fill("构图 34");
  await frame.locator("#gallerySearch").press("Tab");
  await searchResponse;
  const clearResponse = responseFor(page, "/gallery/list", (response) => {
    const params = new URL(response.url()).searchParams;
    return !params.get("query") && params.get("sources") === '["command"]' && Number(params.get("offset")) === 0;
  });
  await frame.locator("#galleryClearSearch").click(); await clearResponse;
  assert.equal(await frame.locator("#gallerySearch").inputValue(), "");
  assert.equal(await frame.locator("#gallerySource").inputValue(), "command");
  assert.match(await frame.locator("#selectionCount").textContent(), /2/, "clearing search discarded selected records");
  await frame.locator("#cancelSelectionButton").click();
  const allResponse = responseFor(page, "/gallery/list", (response) => !new URL(response.url()).searchParams.get("sources"));
  await selectValue(frame, inner, "gallerySource", ""); await allResponse;
}

async function testDetailLoading(page, frame, inner, name) {
  await frame.locator(".gallery-card").first().waitFor();
  let release;
  const wait = new Promise((resolve) => { release = resolve; });
  const pattern = "**/gallery/detail/*";
  const delay = async (route) => { await wait; await route.continue(); };
  await page.route(pattern, delay);
  try {
    await inner.locator(".gallery-card .gallery-info").first().evaluate((item) => item.scrollIntoView({ block: "center" }));
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator(".detail-loading").waitFor(); await settle(inner);
    const drawer = await frame.locator("#detailDrawer").boundingBox();
    const loading = await frame.locator("#detailFooter").boundingBox();
    assert.ok(Math.abs(drawer.y + drawer.height - loading.y - loading.height) < 1.5, `${name}: loading footer not anchored ${JSON.stringify({ drawer, loading })}`);
    await capture(page, inner, `${name}-detail-loading`);
    release();
    await frame.locator("[data-copy-field]").first().waitFor(); await settle(inner);
    const loaded = await frame.locator("#detailFooter").boundingBox();
    assert.ok(Math.abs(loaded.y - loading.y) < 1.5, `${name}: footer moved after loading`);
    await capture(page, inner, `${name}-detail-loaded`);
    await frame.locator("#closeDrawer").click();
  } finally { release(); await page.unroute(pattern, delay); }
}

async function testGalleryOverlays(page, frame, inner, name) {
  const listing = await api(page, "get", "gallery/list?limit=60");
  const multi = listing.items.find((item) => Number(item.image_count) > 1);
  assert.ok(multi, "isolated harness must contain a multi-image record");
  await api(page, "post", "gallery/favorite", { generation_id: multi.id, favorite: true });
  const reload = responseFor(page, "/gallery/list"); await frame.locator("#galleryRefresh").click(); await reload;
  let card = frame.locator(`[data-gallery-id="${multi.id}"]`);
  if (!await card.count()) {
    await frame.locator("#galleryNext:not(:disabled)").click();
    await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
    card = frame.locator(`[data-gallery-id="${multi.id}"]`);
  }
  await card.waitFor();
  await card.locator(".gallery-selection").click();
  const styles = await inner.locator(`[data-gallery-id="${multi.id}"]`).evaluate((element) => {
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1;
    const context = canvas.getContext("2d");
    return [".gallery-source-label", ".gallery-image-count", ".gallery-selection > span", ".gallery-favorite"].map((selector) => {
      const style = getComputedStyle(element.querySelector(selector));
      context.clearRect(0, 0, 1, 1); context.fillStyle = style.backgroundColor; context.fillRect(0, 0, 1, 1);
      return { selector, blur: style.backdropFilter, alpha: context.getImageData(0, 0, 1, 1).data[3] };
    });
  });
  for (const item of styles) {
    assert.match(item.blur, /blur\(14px\)/, `${name} ${item.selector}: missing shared background blur`);
    if (item.selector === ".gallery-selection > span") assert.equal(item.alpha, 255, `${name}: checked selection must use the unmodified opaque theme color`);
    else assert.ok(item.alpha > 20 && item.alpha < 245, `${name} ${item.selector}: overlay should remain translucent, alpha ${item.alpha}`);
  }
  await inner.locator(`[data-gallery-id="${multi.id}"]`).evaluate((element) => element.scrollIntoView({ block: "center" }));
  await capture(page, inner, `${name}-gallery-overlays-checked`);
  await frame.locator("#cancelSelectionButton").click();
  await api(page, "post", "gallery/favorite", { generation_id: multi.id, favorite: false });
  console.log(`${name}: gallery overlay styles ${JSON.stringify(styles)}`);
}

async function paste(frame, object) {
  await frame.locator("#pasteParametersButton").click();
  await frame.locator("#pasteParametersInput").fill(JSON.stringify(object));
  await frame.getByRole("button", { name: "读取参数", exact: true }).click();
}

async function testPaste(page, frame, inner, name) {
  await frame.locator('[data-view="generate"]').click();
  const sameRow = await inner.locator("#pasteParametersButton").evaluate((button) => button.closest(".generation-mode-row")?.contains(document.querySelector(".mode-tabs")));
  assert.equal(sameRow, true, "paste button must be beside generation mode selector");
  await frame.locator('[data-mode="img2img"]').click();
  await paste(frame, { format: "image_studio", version: 1, generation_engine: "nai", data: { model_ref: "nai:nai-diffusion-4-5-full", mode: "text2img", prompt: "landscape, clear daylight", negative_prompt: "", parameters: { cfg: 0, artist: "", steps: 20, unsupported_test_parameter: false } } });
  await inner.waitForFunction(() => document.getElementById("modelChoice").value === "nai:nai-diffusion-4-5-full");
  assert.equal(await frame.locator('[data-mode="text2img"]').getAttribute("class").then((value) => value.includes("is-active")), true);
  assert.equal(await frame.locator('[data-model-parameter="cfg"]').inputValue(), "0");
  await frame.locator("#parameterImportNotice:not(.is-hidden)").waitFor();
  const details = frame.locator("#parameterImportNotice details");
  assert.equal(await details.evaluate((item) => item.open), false, "unmapped parameters must start collapsed");
  await details.locator("summary").click();
  assert.equal(await details.evaluate((item) => item.open), true);
  await capture(page, inner, `${name}-paste-warning`);
  await frame.locator("[data-dismiss-parameter-notice]").click();
  await frame.locator("#parameterImportNotice").waitFor({ state: "hidden" });
  await paste(frame, { format: "image_studio", version: 1, generation_engine: "openai_images", data: { model_ref: "natural:studio-image", mode: "img2img", prompt: "Change the image lighting", parameters: { size: "1024x1024", quality: "high" } } });
  await inner.waitForFunction(() => document.getElementById("modelChoice").value === "natural:studio-image");
  assert.equal(await frame.locator('[data-mode="img2img"]').getAttribute("class").then((value) => value.includes("is-active")), true);
  await geometry(inner, `${name}-paste`);
}

async function testImportDate(page, frame, inner, name) {
  await frame.locator('[data-view="import"]').click();
  const before = Date.now();
  await inner.evaluate(async () => {
    const canvas = document.createElement("canvas"); canvas.width = 8; canvas.height = 8;
    canvas.getContext("2d").fillRect(0, 0, 8, 8);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const file = new File([blob], "metadata-free-old-mtime.png", { type: "image/png", lastModified: 978307200000 });
    const transfer = new DataTransfer(); transfer.items.add(file);
    const input = document.getElementById("importFiles"); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  const date = await frame.locator('[data-import-field="generated_at"]').first().inputValue();
  assert.ok(date, "metadata-free import must prefill the current time");
  const parsed = await inner.evaluate((value) => new Date(value).getTime(), date);
  assert.ok(parsed >= before - 90000 && parsed <= Date.now() + 90000, `current date fallback is not current: ${date}`);
  await capture(page, inner, `${name}-import-date`);
  await frame.locator("#cancelImportButton").click();
}

async function testQuota(page, frame, inner, name) {
  await frame.locator('[data-view="settings"]').click();
  await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  assert.equal(await frame.locator("#historyRecords").getAttribute("min"), "0");
  const beforeRecords = await frame.locator("#historyRecords").inputValue();
  const beforeBytes = await frame.locator("#historyMegabytes").inputValue();
  await frame.locator("#storageQuotaRecords").waitFor(); await frame.locator("#storageQuotaBytes").waitFor();
  for (const id of ["storageQuotaRecords", "storageQuotaBytes"]) assert.ok(await frame.locator(`#${id} progress, #${id} [role="progressbar"]`).count(), `${id} lacks an accessible progress meter`);
  assert.ok(await frame.locator("#storageQuotaRecordsExempt").textContent());
  assert.ok(await frame.locator("#storageQuotaBytesExempt").textContent());
  await inner.locator("#storageQuotaRecords").evaluate((item) => item.scrollIntoView({ block: "center" }));
  await capture(page, inner, `${name}-quota`);
  await frame.locator("#historyRecords").fill("0");
  await frame.locator("#historyRecords").press("Tab");
  await frame.locator("#storageQuotaRecords").waitFor({ state: "hidden" });
  await frame.locator("#historyMegabytes").fill("0");
  await frame.locator("#historyMegabytes").press("Tab");
  await frame.locator("#storageQuotaBytes").waitFor({ state: "hidden" });
  await capture(page, inner, `${name}-quota-unlimited`);
  await frame.locator("#historyRecords").fill(beforeRecords); await frame.locator("#historyMegabytes").fill(beforeBytes);
  await frame.locator("#historyMegabytes").press("Tab");
  await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [
      { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
      { name: "compact-dark", width: 1100, height: 900, theme: "dark" },
      { name: "mobile-light", width: 390, height: 844, theme: "light" },
      { name: "narrow-dark", width: 360, height: 800, theme: "dark" },
    ]) {
      const page = await browser.newPage({ viewport: { width: test.width, height: test.height }, hasTouch: test.width < 600 });
      page.setDefaultTimeout(12000);
      const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.addInitScript(() => { try { Object.defineProperty(navigator, "clipboard", { configurable: true, value: { readText: () => Promise.reject(new Error("Test clipboard permission denied")) } }); } catch {} });
      await page.goto(base);
      assert.equal(await page.locator("#studio").count(), 1, "Expected the isolated harness iframe");
      const frame = page.frameLocator("#studio");
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const inner = page.frames().find((item) => item.url().includes("/ui/"));
      await inner.evaluate(async (theme) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme);
      await testSelectionActions(page, frame, inner, test.name);
      await testDetailLoading(page, frame, inner, test.name);
      await testGalleryOverlays(page, frame, inner, test.name);
      await testPaste(page, frame, inner, test.name);
      await testImportDate(page, frame, inner, test.name);
      await testQuota(page, frame, inner, test.name);
      assert.deepEqual(errors, [], `${test.name}: page errors`);
      await page.close(); console.log(`${test.name}: batch favorites, clear search, loading footer, paste, import date, quotas passed`);
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
