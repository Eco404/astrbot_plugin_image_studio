/* Gated real-API regressions; run only against tests/support/webui_harness.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-gallery-loading-"));

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
}
async function frames(frame, count = 6) {
  await frame.evaluate(remaining => new Promise(resolve => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick);
    requestAnimationFrame(tick);
  }), count);
}
async function started(control) {
  let timeout;
  try {
    return await Promise.race([
      control.started.promise,
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error(`No matching gallery request: ${control.name}`)), 10000); }),
    ]);
  } finally { clearTimeout(timeout); }
}
async function settled(frame) {
  await frame.locator('#galleryGrid[aria-busy="false"]').waitFor({ state: "attached" });
  await frames(frame);
}
async function imageReady(frame, id) {
  await frame.waitForFunction(value => {
    const card = [...document.querySelectorAll("[data-gallery-id]")].find(item => item.dataset.galleryId === value);
    const image = card?.querySelector(".gallery-image-wrap img");
    return image?.complete && image.naturalWidth > 0 && !card.querySelector(".gallery-image-pending");
  }, id);
}
async function capture(page, frame, name) {
  await frames(frame);
  const layout = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(layout.scroll <= layout.width + 1, `${name}: horizontal overflow ${JSON.stringify(layout)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function run(browser, engine, width) {
  const name = `${engine}-${width}`;
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(12000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const plans = [], controls = [], lists = [], previews = [];
  const firstPreview = gate();
  let firstItem, secondItem, secondFailures = 0;
  const plan = (label, matches, failure = "") => {
    const value = { name: label, matches, failure, started: gate(), release: gate(), finished: gate() };
    controls.push(value); plans.push(value); return value;
  };
  await page.route("**/gallery/list?*", async route => {
    const url = new URL(route.request().url());
    lists.push(url);
    assert.equal(url.searchParams.get("light"), "1", "gallery navigation must request a lightweight manifest");
    const index = plans.findIndex(item => item.matches(url));
    const control = index < 0 ? null : plans.splice(index, 1)[0];
    const response = await route.fetch();
    const body = await response.json();
    const payload = body.data || body;
    assert.ok(payload.items.every(item => !item.thumbnail_data_url), "lightweight manifests must not contain inline thumbnails");
    if (control) { control.started.release(payload); await control.release.promise; }
    try {
      if (control?.failure) await route.fulfill({ status: 503, contentType: "application/json", json: { message: control.failure } });
      else await route.fulfill({ response, json: body });
    } finally { control?.finished.release(); }
  });
  await page.route("**/gallery/image/*", async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("detail") === "preview") {
      const id = decodeURIComponent(url.pathname.split("/").at(-1));
      previews.push(id);
      if (id === firstItem?.image_id) await firstPreview.promise;
      if (id === secondItem?.image_id && secondFailures++ === 0) {
        await route.fulfill({ status: 503, contentType: "application/json", json: { message: "fixture-thumbnail-retry" } });
        return;
      }
    }
    await route.continue();
  });
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(async theme => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, engine === "webkit" ? "dark" : "light");

    // Neither list transport nor image transport may hold back the page shell.
    const initial = plan("initial page", () => true);
    await frame.locator('[data-view="gallery"]').click();
    const firstPage = await started(initial);
    [firstItem, secondItem] = firstPage.items;
    const firstImageId = firstItem.image_id;
    const secondImageId = secondItem.image_id;
    assert.ok(firstImageId && secondImageId, "manifest needs first-image identities for separate preview requests");
    assert.ok(firstPage.total > firstPage.limit, "isolated fixture must have a second page");
    await frame.locator('#galleryGrid[aria-busy="true"]').waitFor();
    assert.equal(await frame.locator("#galleryView").isVisible(), true);
    assert.equal(await frame.locator("#gallerySearch").isEnabled(), true);
    assert.equal(await frame.locator("#galleryLoadingStatus").isVisible(), false, "loading is shown by cards, without a reading-status message");
    assert.equal(await frame.locator("#galleryLoadingStatus").innerText(), "");
    const skeletons = frame.locator(".gallery-card.is-placeholder");
    assert.ok(await skeletons.count() > 0, "cards must reserve their geometry while the list is held");
    assert.equal(await skeletons.locator("input, button, [tabindex], [data-gallery-id]").count(), 0, "placeholder cards must not be interactive");
    assert.equal(await skeletons.evaluateAll(items => items.some(item => item.hasAttribute("data-gallery-id") || item.hasAttribute("tabindex"))), false);
    await capture(page, frame, `${name}-list-pending`);
    initial.release.release();
    await settled(frame);
    const firstCard = frame.locator(`[data-gallery-id="${firstItem.id}"]`);
    const secondCard = frame.locator(`[data-gallery-id="${secondItem.id}"]`);
    await firstCard.locator(".gallery-image-pending").waitFor();
    await secondCard.locator(".gallery-preview-retry").waitFor();
    await imageReady(frame, firstPage.items[2].id);
    assert.equal(await frame.locator(".gallery-card.is-placeholder").count(), 0);
    assert.equal(await frame.locator("[data-gallery-id]").count(), firstPage.items.length);
    const beforeImage = await firstCard.locator(".gallery-image-wrap").boundingBox();
    assert.ok(Math.abs(beforeImage.width - beforeImage.height) < 1, "pending previews must retain square geometry");
    const background = await firstCard.locator(".gallery-image-wrap").evaluate(element => getComputedStyle(element).backgroundColor);
    assert.equal(background, engine === "webkit" ? "rgba(255, 255, 255, 0.05)" : "rgba(255, 255, 255, 0.45)", "pending previews must retain the themed surface, not a black image placeholder");
    assert.equal(await firstCard.locator(".gallery-missing-image").count(), 0, "a pending preview is not a missing image");
    if (width < 600) assert.ok(new Set(previews).size < firstPage.items.length, "offscreen previews should not all load eagerly");
    await capture(page, frame, `${name}-independent-previews`);

    await secondCard.locator(".gallery-preview-retry").click();
    await imageReady(frame, secondItem.id);
    assert.equal(previews.filter(id => id === secondImageId).length, 2, "a failed preview must be individually retryable");
    assert.equal(await frame.locator("#detailDrawer").getAttribute("aria-hidden"), "true", "preview retry must not open the detail drawer");

    // Opening details while its gallery preview is pending must share that request.
    await firstCard.locator(".gallery-info").click();
    await frame.locator("#detailDrawer.is-open").waitFor();
    await frame.locator("#detailCopy:not(:disabled)").waitFor();
    await frames(frame);
    assert.equal(previews.filter(id => id === firstImageId).length, 1, "detail must join an in-flight gallery preview");
    firstPreview.release();
    await frame.locator("#closeDrawer").click();
    await imageReady(frame, firstItem.id);
    const afterImage = await firstCard.locator(".gallery-image-wrap").boundingBox();
    assert.equal(afterImage.height, beforeImage.height, "preview completion must not shift card geometry");
    await firstCard.evaluate(card => { window.__loadingCard = card; window.__loadingImage = card.querySelector("img"); });

    const refresh = plan("same-page refresh", () => true);
    await frame.locator("#galleryRefresh").click();
    await started(refresh);
    await frame.locator('#galleryGrid[aria-busy="true"]').waitFor();
    assert.equal(await frame.locator(".gallery-card.is-placeholder").count(), 0, "refresh of an unchanged page keeps its already loaded cards");
    assert.equal(await firstCard.evaluate(card => card === window.__loadingCard && card.querySelector("img") === window.__loadingImage), true);
    refresh.release.release(); await settled(frame); await imageReady(frame, firstItem.id);
    assert.equal(previews.filter(id => id === firstImageId).length, 1, "same-page refresh must reuse cached previews");

    // Let the newer first-page response finish before the earlier second-page one.
    const older = plan("older next-page request", url => Number(url.searchParams.get("offset")) > 0);
    await frame.locator("#galleryNext").click(); await started(older);
    await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
    assert.ok(await frame.locator(".gallery-card.is-placeholder").count() > 0);
    assert.equal(await frame.locator("#galleryPrev").isEnabled(), true, "navigation stays responsive while a page is loading");
    const newer = plan("newer previous-page request", url => Number(url.searchParams.get("offset")) === 0);
    await frame.locator("#galleryPrev").click(); await started(newer);
    await frame.locator("#galleryPageLabel").filter({ hasText: "第 1" }).waitFor();
    newer.release.release(); await settled(frame); await imageReady(frame, firstItem.id);
    older.release.release(); await older.finished.promise; await frames(frame, 12);
    assert.ok((await frame.locator("#galleryPageLabel").innerText()).includes("第 1"));
    assert.equal(await frame.locator("[data-gallery-id]").first().getAttribute("data-gallery-id"), firstItem.id, "stale page responses cannot replace the current page");

    // A stale failure must also not replace a newer search or show a stale notice.
    const staleFailure = plan("obsolete page failure", url => Number(url.searchParams.get("offset")) > 0, "fixture-stale-list-failure");
    await frame.locator("#galleryNext").click(); await started(staleFailure);
    const searched = plan("new search", url => url.searchParams.get("query") === "构图 1");
    await frame.locator("#gallerySearch").fill("构图 1");
    await frame.locator("#gallerySearch").press("Tab");
    const searchPage = await started(searched);
    assert.ok(searchPage.items.length > 0);
    searched.release.release(); await settled(frame);
    staleFailure.release.release(); await staleFailure.finished.promise; await frames(frame, 12);
    assert.deepEqual(await frame.locator("[data-gallery-id]").evaluateAll(items => items.map(item => item.dataset.galleryId)), searchPage.items.map(item => item.id));
    assert.ok(!(await frame.locator("#appNoticeMessage").innerText()).includes("fixture-stale-list-failure"));
    assert.equal(await frame.locator("#galleryRetry").isVisible(), false);

    // A current failed request leaves a deliberate retry state, not an endless skeleton.
    const failure = plan("current list failure", () => true, "fixture-current-list-failure");
    await frame.locator("#galleryClearSearch").click(); await started(failure);
    failure.release.release(); await settled(frame);
    await frame.locator("#galleryRetry").waitFor();
    assert.equal(await frame.locator(".gallery-card.is-placeholder").count(), 0);
    const retry = plan("list retry", () => true);
    await frame.locator("#galleryRetry").click(); await started(retry);
    assert.equal(await frame.locator("#galleryGrid").getAttribute("aria-busy"), "true");
    retry.release.release(); await settled(frame); await imageReady(frame, firstItem.id);
    assert.equal(await frame.locator("#galleryRetry").isVisible(), false);
    assert.equal(previews.filter(id => id === firstImageId).length, 1, "page and filter revisits must retain preview cache reuse");
    if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
    await capture(page, frame, `${name}-recovered`);
    assert.deepEqual(errors, []);
    assert.ok(lists.length >= 8);
    console.log(`${name}: immediate shell/page transitions, independent lazy previews, request/cache reuse, retries and stale-response isolation passed`);
  } finally {
    firstPreview.release();
    controls.forEach(control => control.release.release());
    await page.close();
  }
}

(async () => {
  const selected = process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"];
  for (const engine of selected) {
    if (!engines[engine]) throw new Error(`Unsupported STUDIO_BROWSER: ${engine}`);
    const browser = await engines[engine].launch({ headless: true });
    try { for (const width of engine === "chromium" ? [1440, 390] : [390]) await run(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
