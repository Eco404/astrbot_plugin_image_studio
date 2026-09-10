/* Exercise PhotoSwipe against isolated synthetic gallery records only. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-mobile-viewer-"));

async function select(frame, inner, id, value) {
  const optionIndex = await inner.locator(`#${id}`).evaluate((element, target) => Array.from(element.options).findIndex((option) => option.value === target), value);
  assert.ok(optionIndex >= 0);
  await frame.locator(`.studio-select-trigger[data-select-id="${id}"]`).click();
  if (await inner.locator(`#${id}`).evaluate(element => element.multiple)) await frame.locator('.studio-select-menu [data-select-action="clear"]').click();
  await frame.locator(`.studio-select-menu [data-option-index="${optionIndex}"]`).click();
  await frame.locator(`.studio-select-trigger[data-select-id="${id}"]`).press("Escape");
}

async function current(inner) {
  return await inner.evaluate(() => {
    const viewer = window.__testViewer; const slide = viewer.currSlide;
    return { index: viewer.currIndex, imageId: slide.data.image_id, original: !!slide.data.originalSrc, preview: !!slide.data.previewSrc, state: slide.content.state, tag: slide.content.element?.tagName, width: slide.content.element?.naturalWidth || 0, zoom: slide.currZoomLevel, x: slide.pan.x, y: slide.pan.y };
  });
}

async function waitLoaded(inner, index, original = true) {
  await inner.waitForFunction(({ position, full }) => {
    const viewer = window.__testViewer; const slide = viewer?.currSlide;
    return viewer?.currIndex === position && slide?.content.state === "loaded" && slide.content.element?.tagName === "IMG" && slide.content.element.naturalWidth > 1 && (!full || !!slide.data.originalSrc);
  }, { position: index, full: original });
}

async function openViewer(frame, inner) {
  await frame.locator("[data-detail-image]").waitFor();
  await frame.locator("[data-detail-image]").click();
  await inner.waitForFunction(() => window.__testViewer?.opener.isOpen);
}

async function screenshot(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function swipe(page, inner, direction) {
  const originalIndex = await inner.evaluate(() => window.__testViewer.currIndex);
  const client = await page.context().newCDPSession(page);
  const width = page.viewportSize().width;
  const from = direction > 0 ? width - 55 : 55; const to = direction > 0 ? 55 : width - 55;
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from, y: 410 }] });
  for (let step = 1; step <= 6; step++) {
    await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: from + (to - from) * step / 6, y: 410 }] });
    await page.waitForTimeout(16);
  }
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }); await client.detach();
  await inner.waitForFunction((index) => window.__testViewer.currIndex === index, originalIndex + direction);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [{ width: 390, theme: "light" }, { width: 320, theme: "dark" }]) {
      const page = await browser.newPage({ viewport: { width: test.width, height: 844 }, hasTouch: true });
      page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#modelChoice:not(:disabled)").waitFor();
      const inner = page.frames().find((item) => item.url().includes("/ui/"));
      await inner.evaluate(async (theme) => {
        await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme });
        const Original = window.PhotoSwipe;
        window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__testViewer = this; } };
      }, test.theme);
      await frame.locator('[data-view="gallery"]').click(); await frame.locator(".gallery-card").first().waitFor();
      const natural = page.waitForResponse((response) => response.url().includes("/gallery/list") && new URL(response.url()).searchParams.get("provider_ids") === '["natural"]');
      await select(frame, inner, "galleryProvider", "natural"); await natural;
      await frame.locator(".gallery-card .gallery-info").first().click(); await openViewer(frame, inner); await waitLoaded(inner, 0);
      assert.equal(await frame.locator(".image-studio-controls-visible").count(), 0, "download button should start hidden");
      await inner.waitForFunction(() => document.querySelector(".image-studio-viewer-backdrop:not(.image-studio-viewer-backdrop-previous)")?.naturalWidth > 1);
      const backdrop = await inner.locator(".image-studio-viewer-backdrop:not(.image-studio-viewer-backdrop-previous)").evaluate((image) => ({ width: image.naturalWidth, fit: getComputedStyle(image).objectFit, filter: getComputedStyle(image).filter, holderFilter: getComputedStyle(image.parentElement).filter, holderClass: image.parentElement.className }));
      assert.ok(backdrop.width > 1); assert.equal(backdrop.fit, "cover"); assert.equal(backdrop.filter, "none"); assert.match(backdrop.holderFilter, /blur\(24px\)/); assert.equal(backdrop.holderClass, "image-studio-viewer-background");
      assert.equal(await inner.locator(".pswp__bg").evaluate((element) => Number(getComputedStyle(element).opacity)), 1, "viewer background must obscure the underlying dialog");
      await screenshot(page, inner, `${test.width}-${test.theme}-initial-background`);

      let releaseError;
      const errorGate = new Promise((resolve) => { releaseError = resolve; });
      const errorRoute = async (route) => { await errorGate; await route.continue(); };
      await page.route("**/gallery/image/*", errorRoute);
      const forcedError = await inner.evaluate(() => {
        window.__testViewer.goTo(3);
        const content = window.__testViewer.currSlide.content;
        content.onError();
        return { tag: content.element?.tagName, state: content.state, message: content.element?.textContent };
      });
      // A ready preview may repair the error in the next microtask; capture the actual error synchronously.
      assert.equal(forcedError.tag, "DIV", "forced error should exercise PhotoSwipe's error element");
      assert.equal(forcedError.state, "error");
      assert.match(forcedError.message, /图片暂时无法加载/);
      assert.doesNotMatch(forcedError.message, /The image cannot be loaded/i);
      await screenshot(page, inner, `${test.width}-${test.theme}-forced-error`);
      releaseError(); await waitLoaded(inner, 3); await page.unroute("**/gallery/image/*", errorRoute);
      await screenshot(page, inner, `${test.width}-${test.theme}-recovered-error`);

      const delayed = async (route) => { await new Promise((resolve) => setTimeout(resolve, 70 + (route.request().url().length % 4) * 25)); await route.continue(); };
      await page.route("**/gallery/image/*", delayed);
      for (const direction of [1, 1, -1, 1, -1, -1]) await swipe(page, inner, direction);
      await waitLoaded(inner, 3);
      for (const index of [8, 2, 7, 3]) await inner.evaluate((target) => window.__testViewer.goTo(target), index);
      await waitLoaded(inner, 3); await page.unroute("**/gallery/image/*", delayed);
      assert.equal((await current(inner)).tag, "IMG");

      let releaseZoom;
      const zoomGate = new Promise((resolve) => { releaseZoom = resolve; });
      const zoomImageId = await inner.evaluate(() => window.__testViewer.options.dataSource[6].image_id);
      const zoomRoute = async (route) => { const url = new URL(route.request().url()); if (url.pathname.endsWith(`/${zoomImageId}`) && url.searchParams.get("detail") === "original") await zoomGate; await route.continue(); };
      await page.route("**/gallery/image/*", zoomRoute);
      await inner.evaluate(() => window.__testViewer.goTo(6)); await waitLoaded(inner, 6, false);
      await inner.evaluate(() => { const viewer = window.__testViewer; viewer.zoomTo(viewer.currSlide.zoomLevels.initial * 2, { x: innerWidth / 2, y: innerHeight / 2 }, 0); });
      const zoomed = await current(inner); releaseZoom(); await waitLoaded(inner, 6);
      const upgraded = await current(inner); assert.ok(Math.abs(upgraded.zoom - zoomed.zoom) < .001); assert.ok(Math.abs(upgraded.x - zoomed.x) < 1 && Math.abs(upgraded.y - zoomed.y) < 1);
      await page.unroute("**/gallery/image/*", zoomRoute);
      await inner.evaluate(() => window.__testViewer.zoomTo(window.__testViewer.currSlide.zoomLevels.initial, undefined, 0));

      let allowOriginal = false; let originalFailures = 0;
      const retryImageId = await inner.evaluate(() => window.__testViewer.options.dataSource[10].image_id);
      const failing = async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith(`/${retryImageId}`) && url.searchParams.get("detail") === "original" && !allowOriginal) { originalFailures++; await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试原图暂时不可用" }) }); }
        else await route.continue();
      };
      await page.route("**/gallery/image/*", failing);
      await inner.evaluate(() => window.__testViewer.goTo(10)); await waitLoaded(inner, 10, false);
      await frame.locator(".image-studio-image-status:not([hidden])").waitFor();
      assert.equal((await current(inner)).original, false); assert.equal((await current(inner)).preview, true);
      await screenshot(page, inner, `${test.width}-${test.theme}-original-retry`);
      allowOriginal = true; await frame.locator(".image-studio-image-status button:not(:disabled)").click(); await waitLoaded(inner, 10);
      await frame.locator(".image-studio-image-status").waitFor({ state: "hidden" }); assert.ok(originalFailures >= 1);
      await page.unroute("**/gallery/image/*", failing);

      const latePreviewId = await inner.evaluate(() => window.__testViewer.options.dataSource[15].image_id);
      const latePreview = async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith(`/${latePreviewId}`) && url.searchParams.get("detail") === "preview") {
          await new Promise((resolve) => setTimeout(resolve, 240));
          await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试延迟预览失败" }) });
        } else await route.continue();
      };
      await page.route("**/gallery/image/*", latePreview);
      await inner.evaluate(() => window.__testViewer.goTo(15)); await waitLoaded(inner, 15); await page.waitForTimeout(300);
      assert.equal(await frame.locator(".image-studio-image-status").isVisible(), false, "a late preview failure must not hide a successful original");
      await page.unroute("**/gallery/image/*", latePreview);

      let releaseOld; const oldGate = new Promise((resolve) => { releaseOld = resolve; });
      const oldImageId = await inner.evaluate(() => window.__testViewer.options.dataSource[1].image_id);
      const oldRequest = page.waitForRequest((request) => { const url = new URL(request.url()); return url.pathname.endsWith(`/${oldImageId}`) && url.searchParams.get("detail") === "original"; });
      const stale = async (route) => { const url = new URL(route.request().url()); if (url.pathname.endsWith(`/${oldImageId}`) && url.searchParams.get("detail") === "original") await oldGate; await route.continue(); };
      await page.route("**/gallery/image/*", stale); await inner.evaluate(() => window.__testViewer.goTo(1));
      await oldRequest;
      await inner.evaluate(() => window.__testViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" }); await frame.locator("#closeDrawer").click();
      await inner.evaluate(() => window.scrollTo(0, 0));
      const filtered = page.waitForResponse((response) => response.url().includes("/gallery/list") && new URL(response.url()).searchParams.get("sources") === '["command"]');
      await select(frame, inner, "gallerySource", "command"); await filtered;
      await frame.locator(".gallery-card .gallery-info").first().click(); await openViewer(frame, inner); await waitLoaded(inner, 0);
      await inner.waitForFunction(() => !!window.__testViewer.options.dataSource[1].previewSrc);
      const fresh = await inner.evaluate(() => ({ id: window.__testViewer.options.dataSource[1].image_id, src: window.__testViewer.options.dataSource[1].src }));
      assert.notEqual(fresh.id, oldImageId); releaseOld(); await page.waitForTimeout(160);
      const afterStale = await inner.evaluate(() => ({ id: window.__testViewer.options.dataSource[1].image_id, src: window.__testViewer.options.dataSource[1].src }));
      assert.deepEqual(afterStale, fresh, "an old viewer's response wrote into a newly opened viewer");
      await page.unroute("**/gallery/image/*", stale);
      assert.equal(await frame.locator(".image-studio-controls-visible").count(), 0);

      await inner.evaluate(() => window.__testViewer.goTo(1)); await waitLoaded(inner, 1);
      const finalImageId = (await current(inner)).imageId;
      const finalGenerationId = await inner.evaluate(() => window.__testViewer.currSlide.data.generation_id);
      await inner.evaluate(() => window.__testViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
      await inner.waitForFunction((id) => document.querySelector('#drawerBody [data-detail-image]') && document.querySelector(`.gallery-card[data-gallery-id="${id}"]`), finalGenerationId);
      await openViewer(frame, inner); await inner.waitForFunction((id) => window.__testViewer.currSlide.data.image_id === id, finalImageId);
      assert.deepEqual(errors, []); await page.close(); console.log(`${test.width}px ${test.theme}: error recovery, rapid swipes, cache, original retry, session isolation, zoom, blurred background passed`);
    }
    console.log(`Synthetic viewer screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
