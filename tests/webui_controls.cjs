/* Real controls against webui_harness.py; files remain browser-only previews. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-controls-"));

async function api(request, method, endpoint, data) {
  const response = await request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${method} ${endpoint}: ${response.status()}`);
  const payload = await response.json();
  return payload.data || payload;
}

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function noOverflow(frame, name) {
  const result = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(result.scroll <= result.width + 1, `${name}: horizontal overflow ${JSON.stringify(result)}`);
}

async function capture(page, frame, name) {
  await settle(frame); await noOverflow(frame, name);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function openView(frame, view) {
  await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await frame.locator(`[data-view="${view}"]`).click();
  if (view === "gallery") await frame.locator(".gallery-card").first().waitFor();
  if (view === "settings") await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  await settle(frame);
}

async function checkShadows(frame, name) {
  const result = await frame.evaluate(() => {
    const exceptions = ".nav-item, .detail-filmstrip-thumb, .parameter-copy, .copy-format-picker .studio-select-trigger, .segment:not(.is-active), .model-tab:not(.is-active)";
    return Array.from(document.querySelectorAll("button, a.primary-button, a.quiet-button")).filter((button) => {
      if (!button.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) || button.matches(exceptions)) return false;
      return true;
    }).map((button) => ({ id: button.id || button.className, shadow: getComputedStyle(button).boxShadow }));
  });
  assert.ok(result.length > 0, `${name}: no visible controls`);
  assert.deepEqual(result.filter((item) => item.shadow === "none"), [], `${name}: controls without edge shadows`);
}

async function roundIcon(frame, selector, name) {
  const value = await frame.locator(selector).evaluate((button) => {
    const style = getComputedStyle(button); const box = button.getBoundingClientRect(); const svg = button.querySelector("svg").getBoundingClientRect();
    return { width: style.width, height: style.height, radius: style.borderRadius, ratio: box.width / box.height, dx: svg.x + svg.width / 2 - box.x - box.width / 2, dy: svg.y + svg.height / 2 - box.y - box.height / 2 };
  });
  assert.equal(value.width, "44px", `${name}: width`); assert.equal(value.height, "44px", `${name}: height`);
  assert.equal(value.radius, "50%", `${name}: circle`); assert.ok(Math.abs(value.ratio - 1) < .01);
  assert.ok(Math.abs(value.dx) < 1 && Math.abs(value.dy) < 1, `${name}: icon alignment ${JSON.stringify(value)}`);
}

async function checkFocus(frame, selector, name) {
  const button = frame.locator(selector);
  await button.focus(); await button.press("Tab"); await button.focus();
  const outline = await button.evaluate((item) => ({ width: getComputedStyle(item).outlineWidth, style: getComputedStyle(item).outlineStyle }));
  assert.ok(parseFloat(outline.width) >= 2 && outline.style !== "none", `${name}: keyboard focus missing`);
}

async function files(frame) {
  const values = await frame.evaluate(() => [0, 1].map((index) => {
    const canvas = document.createElement("canvas"); canvas.width = 96; canvas.height = 72;
    const context = canvas.getContext("2d");
    context.fillStyle = index ? "#abc8c1" : "#c6d8e0"; context.fillRect(0, 0, 96, 72);
    context.fillStyle = "#729586"; context.fillRect(0, 45, 96, 27);
    context.fillStyle = "#8399ac"; context.beginPath(); context.moveTo(0, 47); context.lineTo(38, 12); context.lineTo(76, 47); context.fill();
    return canvas.toDataURL("image/png").split(",")[1];
  }));
  return values.map((value, index) => ({ name: `controls-preview-${index}.png`, mimeType: "image/png", buffer: Buffer.from(value, "base64") }));
}

async function chooseFiles(page, frame, activation, chosen) {
  const wait = page.waitForEvent("filechooser");
  const dropzone = frame.locator("#importDropzone");
  if (activation === "click") await dropzone.click();
  else { await dropzone.focus(); await dropzone.press(activation); }
  const chooser = await wait;
  assert.equal(chooser.isMultiple(), true);
  await chooser.setFiles(chosen);
}

async function importer(page, frame, name, uploaded) {
  await openView(frame, "import");
  assert.equal(await frame.locator("#chooseImportFiles").count(), 0, `${name}: duplicate choose-files command`);
  assert.equal(await frame.locator("#importDropzone").evaluate((item) => item.tagName), "BUTTON");
  assert.equal(await frame.locator("#confirmImportButton").isDisabled(), true);
  const disabled = await frame.locator("#confirmImportButton").evaluate((item) => ({ opacity: Number(getComputedStyle(item).opacity), cursor: getComputedStyle(item).cursor }));
  assert.ok(disabled.opacity < 1 && disabled.cursor === "not-allowed");
  await checkFocus(frame, "#importDropzone", name); await checkShadows(frame, `${name}-empty-import`);
  await capture(page, frame, `${name}-empty-import`);
  const fixtures = await files(frame);
  await chooseFiles(page, frame, "click", [fixtures[0]]);
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  assert.equal(await frame.locator(".import-card").count(), 1);
  assert.equal(await frame.locator("#importDropzone.is-compact").isVisible(), true);
  assert.equal(await frame.locator("#importDropzoneLabel").textContent(), "继续添加图片");
  assert.ok((await frame.locator("#importDropzone").evaluate((item) => item.offsetHeight)) < 100);
  await chooseFiles(page, frame, "Enter", [fixtures[1]]);
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  assert.equal(await frame.locator(".import-card").count(), 2);
  await chooseFiles(page, frame, "Space", [fixtures[0], { ...fixtures[1], name: "same-content-renamed.png" }]);
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  assert.equal(await frame.locator(".import-card").count(), 2, `${name}: same SHA-256 added duplicate cards`);
  await frame.locator("#importSummary").filter({ hasText: "已选择 2 张图片" }).waitFor();
  if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
  await checkShadows(frame, `${name}-staged-import`); await capture(page, frame, `${name}-staged-import`);
  await frame.locator("[data-remove-import]").first().click();
  assert.equal(await frame.locator(".import-card").count(), 1);
  await frame.locator("#cancelImportButton").click();
  assert.equal(await frame.locator(".import-card").count(), 0);
  assert.equal(await frame.locator("#importDropzone.is-compact").count(), 0);
  assert.equal(await frame.locator("#confirmImportButton").isDisabled(), true);
  assert.equal(uploaded.count, 0, `${name}: previews must not upload files`);
}

async function gallery(page, frame, name, favoriteId) {
  await openView(frame, "gallery");
  for (const id of ["galleryFavorite", "galleryRefresh"]) await roundIcon(frame, `#${id}`, `${name}-${id}`);
  await checkFocus(frame, "#galleryFavorite", name);
  await checkShadows(frame, `${name}-gallery`);
  await frame.locator("#galleryNext:not(:disabled)").click();
  await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
  const onResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.endsWith("/gallery/list") && url.searchParams.get("favorite") === "true" && url.searchParams.get("offset") === "0";
  });
  await frame.locator("#galleryFavorite").click();
  const selected = await (await onResponse).json();
  assert.ok(selected.items.length > 0 && selected.items.every((item) => item.is_favorite));
  assert.ok(selected.items.some((item) => item.id === favoriteId));
  await frame.locator(`.gallery-card[data-gallery-id="${favoriteId}"]`).waitFor();
  assert.equal(await frame.locator("#galleryFavorite").getAttribute("aria-pressed"), "true");
  assert.equal(await frame.locator("#galleryFavorite").evaluate((button) => button.value), "true");
  assert.equal(await frame.locator(".gallery-card:not(.is-favorite)").count(), 0);
  const pressed = await frame.locator("#galleryFavorite").evaluate((button) => ({ active: button.classList.contains("is-active"), filled: getComputedStyle(button.querySelector("svg")).fill !== "none" }));
  assert.equal(pressed.active, true); assert.equal(pressed.filled, true);
  await capture(page, frame, `${name}-favorites`);
  const offResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.endsWith("/gallery/list") && url.searchParams.get("favorite") === "" && url.searchParams.get("offset") === "0";
  });
  await frame.locator("#galleryFavorite").click();
  const all = await (await offResponse).json();
  await frame.locator("#galleryPageLabel").filter({ hasText: "第 1" }).waitFor();
  assert.ok(all.total > selected.total);
  assert.equal(await frame.locator("#galleryFavorite").getAttribute("aria-pressed"), "false");
  assert.equal(await frame.locator("#galleryFavorite").evaluate((button) => button.value), "");
  const refreshResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/gallery/list"));
  await frame.locator("#galleryRefresh").click(); await refreshResponse;
  await capture(page, frame, `${name}-gallery`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const seedContext = await browser.newContext();
  let favorite;
  try {
    const home = await seedContext.request.get(base);
    assert.match(await home.text(), /<iframe id="studio"/, "expected the isolated harness");
    const existing = await api(seedContext.request, "get", "gallery/list?limit=1&offset=0");
    favorite = existing.items[0]; assert.ok(favorite);
    await api(seedContext.request, "post", "gallery/favorite", { generation_id: favorite.id, favorite: true });
    const cases = [1440, 1100, 900, 720, 390, 320].flatMap((width) => ["light", "dark"].map((theme) => ({ width, theme, zoom: 1 })));
    cases.push({ width: 1440, theme: "light", zoom: 2 });
    for (const test of cases) {
      // 720 CSS pixels at DPR 2 exercise the reflow of a 1440px screen at 200% zoom.
      const name = `${test.width}-${test.theme}${test.zoom === 2 ? "-200pct-equivalent" : ""}`;
      const context = await browser.newContext({ viewport: { width: test.width / test.zoom, height: (test.width < 600 ? 844 : 1000) / test.zoom }, deviceScaleFactor: test.zoom, hasTouch: test.width < 600, reducedMotion: test.theme === "dark" ? "reduce" : "no-preference" });
      const page = await context.newPage(); page.setDefaultTimeout(12000);
      const errors = []; const uploaded = { count: 0 };
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      page.on("request", (request) => {
        const endpoint = new URL(request.url()).pathname;
        if (/\/imports\/(?:prepare$|upload\/|group\/.*\/commit$)/.test(endpoint)) uploaded.count++;
      });
      await page.goto(base); const frame = page.frames().find((item) => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(async (test) => {
        await window.ImageStudioAppearance.ready;
        window.ImageStudioAppearance.set({ preference: test.theme });
        await window.ImageStudioAppearance.saved();
      }, test);
      await gallery(page, frame, name, favorite.id);
      await importer(page, frame, name, uploaded);
      await openView(frame, "settings");
      await checkShadows(frame, `${name}-settings`);
      await frame.locator("#appearanceSettings").scrollIntoViewIfNeeded();
      await capture(page, frame, `${name}-settings`);
      await openView(frame, "generate");
      await checkShadows(frame, `${name}-generate`);
      const segment = await frame.locator(".segment.is-active").evaluate((button) => getComputedStyle(button).boxShadow);
      assert.ok(segment.includes("inset"), `${name}: selected segment lost its inset`);
      await capture(page, frame, `${name}-generate`);
      assert.deepEqual(errors, [], `${name}: browser errors`);
      await context.close();
      console.log(`${name}: round filters, real favorites, file chooser, dedup, shadows and containment passed`);
    }
    console.log(`Screenshots: ${output}`);
  } finally {
    if (favorite) await api(seedContext.request, "post", "gallery/favorite", { generation_id: favorite.id, favorite: favorite.is_favorite });
    await seedContext.close(); await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
