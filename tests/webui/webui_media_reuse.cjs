/* Cross-view request budgets; use only the isolated tests/support/webui_harness.py API. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-media-reuse-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(3):
 width,height=((480,640),(640,480),(560,560))[index]
 image=Image.new("RGB",(width,height),(162+index*15,202,219-index*12));draw=ImageDraw.Draw(image)
 draw.rectangle((0,height*.64,width,height),fill=(90+index*20,144,125));draw.polygon([(0,height*.72),(width*.35,height*.24),(width*.76,height*.72)],fill=(110,134+index*14,159));draw.ellipse((width*.73,height*.1,width*.9,height*.24),fill=(235,239,215))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"safe landscape {marker} image {index}","model":"media-reuse-model","uc":"blur","steps":24,"seed":1000+index,"width":width,"height":height,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}
async function body(response) { const value = await response.json(); return value.data || value; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  return await body(response);
}
async function seed(page, files) {
  const items = files.map((file, index) => ({ client_id: `reuse_${Date.now().toString(36)}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "media-reuse-model" } }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: true });
  assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.notEqual((await body(response)).allowed, false);
  }
  const committed = await api(page, "post", prepared.commit_endpoint, {});
  assert.equal(committed.allowed, true); assert.equal(committed.generation_ids.length, 1);
  return committed.generation_ids[0];
}
async function frames(inner, count = 6) {
  await inner.evaluate((remaining) => new Promise((resolve) => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick);
    requestAnimationFrame(tick);
  }), count);
}
async function selected(inner, index, marker) {
  await inner.locator(`[data-detail-dot="${index}"][aria-current="true"]`).waitFor();
  await inner.locator(".detail-parameter-row").filter({ hasText: `safe landscape ${marker} image ${index}` }).first().waitFor();
  await inner.locator("#detailCopy:not(:disabled)").waitFor();
}
async function viewerReady(inner, id, detail = "display") {
  await inner.waitForFunction(({ imageId, detail }) => {
    const viewer = window.__reuseViewer;
    const image = viewer?.currSlide?.content?.element;
    return viewer?.opener.isOpen && viewer.currSlide.data.image_id === imageId
      && viewer.currSlide.data.loadedDetail === detail && image?.complete && image.naturalWidth > 1;
  }, { imageId: id, detail });
}
async function zoom(inner, enlarged = true) {
  await inner.evaluate(enlarged => {
    const slide = window.__reuseViewer.currSlide;
    slide.zoomTo(enlarged ? slide.zoomLevels.secondary : slide.zoomLevels.initial, undefined, 0);
  }, enlarged);
}
async function openViewer(inner) {
  await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; });
  await inner.locator("[data-detail-image]").click();
  await inner.waitForFunction(() => window.__reuseViewer?.opener.isOpen);
}
async function closeViewer(inner) {
  await inner.evaluate(() => window.__reuseViewer.close());
  await inner.locator(".pswp--open").waitFor({ state: "detached" });
}
async function refresh(page, inner) {
  const refreshed = page.waitForResponse((response) => response.url().includes("/gallery/list?"));
  await inner.locator("#galleryRefresh").evaluate((button) => button.click());
  await refreshed; await frames(inner, 12);
}

async function cacheBoundaries(page, inner, image) {
  const revisions = await inner.evaluate(async (item) => {
    const hooks = window.__reuseHooks;
    const earlier = { ...item, thumbnail_revision: "test-preview-earlier" };
    const later = { ...item, thumbnail_revision: "test-preview-later" };
    hooks.cacheImageMedia(earlier, "preview", "data:image/webp;base64,earlier");
    const missingBeforeLoad = !hooks.getImageMedia(later, "preview");
    const loaded = await hooks.loadImageMedia(later, "preview");
    return { missingBeforeLoad, loaded: loaded.startsWith("data:image/"), earlier: hooks.getImageMedia(earlier, "preview"), sameAfterLoad: hooks.getImageMedia(later, "preview") === loaded };
  }, image);
  assert.deepEqual(revisions, { missingBeforeLoad: true, loaded: true, earlier: "data:image/webp;base64,earlier", sameAfterLoad: true }, "a changed thumbnail revision must fetch its own preview without replacing the previous version");
  const displayRevisions = await inner.evaluate(async item => {
    const hooks = window.__reuseHooks;
    const earlier = { ...item, thumbnail_revision: "test-display-earlier" };
    const later = { ...item, thumbnail_revision: "test-display-later" };
    hooks.cacheImageMedia(earlier, "display:1024", "data:image/webp;base64,earlier");
    const missingBeforeLoad = !hooks.getImageMedia(later, "display:1024");
    const loaded = await hooks.loadImageMedia(later, "display:1024");
    return { missingBeforeLoad, loaded: loaded.startsWith("data:image/"), earlier: hooks.getImageMedia(earlier, "display:1024"), sameAfterLoad: hooks.getImageMedia(later, "display:1024") === loaded, otherSizeMissing: !hooks.getImageMedia(later, "display:1536") };
  }, image);
  assert.deepEqual(displayRevisions, { missingBeforeLoad: true, loaded: true, earlier: "data:image/webp;base64,earlier", sameAfterLoad: true, otherSizeMissing: true }, "display reuse must respect both revision and requested pixel budget");

  let attempts = 0;
  const retryRoute = `**/gallery/image/${image.id}?*`;
  await page.route(retryRoute, async (route) => {
    if (new URL(route.request().url()).searchParams.get("detail") === "original" && ++attempts === 1) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "测试：可重试的原图读取错误" }) });
    } else await route.continue();
  });
  try {
    const retry = await inner.evaluate(async (item) => {
      const hooks = window.__reuseHooks;
      // A new immutable identity exercises the failed-load path independently
      // of originals that a preceding navigation may legitimately have cached.
      const request = { ...item, sha256: `retry-probe-${item.sha256}` };
      let failed = false;
      try { await hooks.loadImageMedia(request, "original"); } catch { failed = true; }
      const result = await hooks.loadImageMedia(request, "original");
      return { failed, recovered: result.startsWith("data:image/"), reusable: hooks.getImageMedia(request, "original") === result };
    }, image);
    assert.deepEqual(retry, { failed: true, recovered: true, reusable: true }, "a failed pending request must be removed so a later retry can recover");
    assert.equal(attempts, 2);
  } finally { await page.unroute(retryRoute); }

  const budget = await inner.evaluate(() => {
    const hooks = window.__reuseHooks;
    const displayed = document.querySelector(".detail-image-frame > [data-detail-image]");
    const displayedSource = displayed.src;
    hooks.cacheImageMedia({ sha256: "budget-probe-0" }, "original", "0".repeat(2 * 1024 * 1024));
    for (let index = 1; index < 12; index++) hooks.cacheImageMedia({ sha256: `budget-probe-${index}` }, "original", String(index % 10).repeat(2 * 1024 * 1024));
    const retained = Array.from({ length: 12 }, (_, index) => !!hooks.getImageMedia({ sha256: `budget-probe-${index}` }, "original"));
    const tooLarge = "x".repeat(17 * 1024 * 1024);
    const returned = hooks.cacheImageMedia({ sha256: "budget-probe-oversized" }, "original", tooLarge);
    return { evictedOldest: !retained[0], retainedNewest: retained.at(-1), retainedCount: retained.filter(Boolean).length, displayedUnchanged: displayed === document.querySelector(".detail-image-frame > [data-detail-image]") && displayed.src === displayedSource && displayed.complete && displayed.naturalWidth > 1, oversizedDisplayable: returned === tooLarge, oversizedNotRetained: !hooks.getImageMedia({ sha256: "budget-probe-oversized" }, "original"), newestSurvivesOversized: !!hooks.getImageMedia({ sha256: "budget-probe-11" }, "original") };
  });
  assert.ok(budget.evictedOldest && budget.retainedNewest && budget.retainedCount < 12, "cache must evict old content once its byte budget is exceeded");
  assert.ok(budget.displayedUnchanged, "eviction must not alter sources already owned by displayed content");
  assert.ok(budget.oversizedDisplayable && budget.oversizedNotRetained && budget.newestSurvivesOversized, "an oversized original may display without flushing reusable smaller images");
}

async function invalidDecodeRecovery(page, groupId, image, marker) {
  const started = gate(); const corruptResponse = gate();
  const pattern = `**/gallery/image/${image.id}?*`;
  const corrupt = "data:image/png;base64,AAAA";
  let attempts = 0;
  await page.route(pattern, async (route) => {
    if (new URL(route.request().url()).searchParams.get("detail") !== "original") { await route.continue(); return; }
    attempts++;
    if (attempts === 1) {
      started.release(); await corruptResponse.promise;
      const response = await route.fetch();
      const payload = await response.json();
      (payload.data || payload).data_url = corrupt;
      await route.fulfill({ response, json: payload });
    } else await route.continue();
  });
  try {
    // A fresh document provides a real decode failure, with no earlier good
    // original masking the corrupted HTTP-200 response through shared reuse.
    await page.reload();
    const inner = page.frames().find((frame) => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.evaluate(() => {
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__reuseViewer = this; } };
    });
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await inner.locator("#detailCopy:not(:disabled)").waitFor();
    await openViewer(inner); await viewerReady(inner, image.id);
    assert.equal(attempts, 0, "fit browsing must not request the original, including before a retry scenario");
    await zoom(inner); await started.promise; corruptResponse.release();
    await viewerReady(inner, image.id, "original");
    assert.equal(attempts, 2, "a malformed HTTP-200 image must be decoded, evicted and fetched again by the viewer retry");
    const recovered = await inner.evaluate(async ({ item, broken }) => {
      const source = window.__reuseHooks.getImageMedia(item, "original");
      const mounted = window.__reuseViewer.currSlide.content.element.src;
      if (!source || source === broken || !mounted.startsWith("blob:")) return false;
      const bytes = new Uint8Array(await (await fetch(mounted)).arrayBuffer());
      const original = atob(source.slice(source.indexOf(",") + 1));
      return bytes.length === original.length && bytes.every((value, index) => value === original.charCodeAt(index));
    }, { item: image, broken: corrupt });
    assert.equal(recovered, true, "the recovered original must replace the corrupt cache entry and reach the mounted viewer image");
    await closeViewer(inner); await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    assert.equal(attempts, 2, "returning to detail fit view must not fetch the original again");
    await openViewer(inner); await viewerReady(inner, image.id); await zoom(inner); await viewerReady(inner, image.id, "original");
    assert.equal(attempts, 2, "reopening and zooming must reuse the recovered original bytes");
    await closeViewer(inner);
  } finally { corruptResponse.release(); await page.unroute(pattern); }
}

async function run(engine) {
  const marker = `${path.basename(output)}-${engine}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const browser = await engines[engine].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  page.setDefaultTimeout(15000);
  await page.addInitScript(() => {
    let factory;
    Object.defineProperty(window, "ImageStudioLibrary", {
      configurable: true,
      get: () => factory,
      set: (value) => { factory = (hooks) => { window.__reuseHooks = hooks; return value(hooks); }; },
    });
  });
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  const requests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    const match = url.pathname.match(/\/gallery\/(image-info|image)\/([^/]+)$/);
    if (match) requests.push({ kind: match[1] === "image" ? url.searchParams.get("detail") || "original" : "metadata", id: decodeURIComponent(match[2]), preview: url.searchParams.get("include_preview") });
    if (url.pathname.endsWith("/gallery/image-sequence")) requests.push({ kind: "sequence", query: url.searchParams.get("query") });
  });
  const count = (kind, id) => requests.filter((item) => item.kind === kind && (!id || item.id === id)).length;
  const originalGate = gate(); const originalStarted = gate();
  const displayGate = gate(); const displayStarted = gate();
  let groupId;
  try {
    groupId = await seed(page, files);
    const manifest = await api(page, "get", `gallery/detail/${groupId}?light=1`);
    const ids = manifest.images.map((item) => item.id);
    await page.route(`**/gallery/image/${ids[0]}?*`, async (route) => {
      if (new URL(route.request().url()).searchParams.get("detail") === "original") { originalStarted.release(); await originalGate.promise; }
      if (new URL(route.request().url()).searchParams.get("detail") === "display") { displayStarted.release(); await displayGate.promise; }
      try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
    });
    await page.goto(base);
    const inner = page.frames().find((frame) => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.evaluate(() => {
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__reuseViewer = this; } };
    });
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.waitForFunction(id => {
      const image = document.querySelector(`[data-gallery-id="${id}"] .gallery-image-wrap img`);
      return image?.complete && image.naturalWidth > 0;
    }, groupId);
    // Gallery previews are separate requests now; detail must reuse the image
    // that has arrived, without adding any preview request of its own.
    requests.length = 0;
    await inner.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await selected(inner, 0, marker); await displayStarted.promise; await frames(inner, 12);
    assert.equal(count("preview", ids[0]), 0, "the gallery cover must seed the shared thumbnail cache");
    assert.ok(requests.filter((item) => item.kind === "metadata").every((item) => item.preview === "0"), "metadata reads must exclude duplicate preview bytes");
    assert.equal(count("metadata", ids[0]), 1);
    assert.equal(count("display", ids[0]), 1);
    assert.equal(count("original"), 0, "detail fit browsing must request only bounded display media");
    const initialSequences = count("sequence"); assert.ok(initialSequences > 0);

    // The bounded display is still pending in detail when the lightbox joins it.
    await openViewer(inner); await page.waitForTimeout(450);
    assert.equal(count("display", ids[0]), 1, "detail and lightbox must join the same pending display request");
    assert.equal(count("original"), 0, "opening the lightbox must not upgrade to original without zoom");
    assert.equal(count("preview", ids[0]), 0);
    assert.equal(count("sequence"), initialSequences, "lightbox must reuse the detail browse sequence");
    displayGate.release(); await viewerReady(inner, ids[0]);
    await zoom(inner); await originalStarted.promise;
    await inner.evaluate(item => { window.__reuseJoinedOriginal = window.__reuseHooks.loadImageMedia(item, "original"); }, manifest.images[0]);
    await frames(inner, 8);
    assert.equal(count("original", ids[0]), 1, "zoom and another original consumer must share the pending original request");
    originalGate.release(); await viewerReady(inner, ids[0], "original");
    assert.equal(await inner.evaluate(async () => (await window.__reuseJoinedOriginal).startsWith("data:image/")), true, "non-viewer consumers retain the data URL contract");
    await zoom(inner, false); await viewerReady(inner, ids[0]);
    assert.equal(count("display", ids[0]), 1, "zooming back to fit must reuse the original bounded display image");
    await inner.evaluate(() => window.__reuseViewer.goTo(2)); await viewerReady(inner, ids[2]);
    await frames(inner, 8);
    assert.equal(count("original", ids[2]), 0, "navigating to a new image at fit resolution must not fetch its original");
    await zoom(inner); await viewerReady(inner, ids[2], "original");
    const beforeReturn = { preview: count("preview", ids[2]), display: count("display", ids[2]), original: count("original", ids[2]) };
    assert.equal(beforeReturn.original, 1);
    await closeViewer(inner); await selected(inner, 2, marker); await inner.locator("#detailUseReference:not(:disabled)").waitFor(); await frames(inner, 8);
    assert.equal(count("preview", ids[2]), beforeReturn.preview, "returning from the lightbox must reuse its selected preview");
    assert.equal(count("display", ids[2]), beforeReturn.display, "returning from zoom to detail must reuse its bounded display");
    assert.equal(count("original", ids[2]), beforeReturn.original, "returning from the lightbox must not request another original");

    await inner.locator("#closeDrawer").click();
    const beforeReopen = { preview: count("preview"), display: count("display"), original: count("original"), metadata: count("metadata", ids[0]), sequence: count("sequence") };
    await inner.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await selected(inner, 0, marker); await inner.locator("#detailUseReference:not(:disabled)").waitFor(); await frames(inner, 8);
    assert.equal(count("preview"), beforeReopen.preview, "closing and reopening detail must retain reusable previews");
    assert.equal(count("original"), beforeReopen.original, "closing and reopening detail must retain reusable originals");
    assert.equal(count("display"), beforeReopen.display, "closing and reopening detail must retain reusable display images");
    assert.equal(count("metadata", ids[0]), beforeReopen.metadata + 1, "mutable parameters must be revalidated on reopening detail");
    assert.equal(count("sequence"), beforeReopen.sequence);

    await refresh(page, inner);
    await openViewer(inner); await viewerReady(inner, ids[0]);
    await zoom(inner); await viewerReady(inner, ids[0], "original");
    assert.equal(count("original"), beforeReopen.original, "zooming in a new lightbox must reuse previously loaded original bytes");
    assert.equal(count("sequence"), beforeReopen.sequence, "unchanged gallery refresh must preserve the reusable browse sequence");
    await closeViewer(inner); await selected(inner, 0, marker);

    const beforeFavorite = count("sequence");
    const favoriteRefresh = page.waitForResponse((response) => response.url().includes("/gallery/list?"));
    await inner.locator("#detailFavorite").click(); await favoriteRefresh; await frames(inner, 10);
    await openViewer(inner); await viewerReady(inner, ids[0]);
    assert.ok(count("sequence") > beforeFavorite, "successful favorite mutation must invalidate browse data");
    await closeViewer(inner); await selected(inner, 0, marker);

    const beforeExternal = count("sequence");
    await api(page, "post", "gallery/images/delete", { generation_id: groupId, image_ids: [ids[2]] });
    await refresh(page, inner); await openViewer(inner); await viewerReady(inner, ids[0]);
    assert.ok(count("sequence") > beforeExternal, "an external gallery mutation detected by refresh must invalidate browse data");
    assert.deepEqual(await inner.evaluate(() => window.__reuseViewer.options.dataSource.map((item) => item.image_id)), ids.slice(0, 2), "lightbox must use actual remaining membership after an external deletion");
    await closeViewer(inner);

    // A changed ordinal must never change which immutable image is selected.
    await inner.locator('[data-detail-dot="1"]').click(); await selected(inner, 1, marker);
    await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    await api(page, "post", "gallery/images/delete", { generation_id: groupId, image_ids: [ids[0]] });
    await refresh(page, inner); await openViewer(inner); await viewerReady(inner, ids[1]);
    assert.deepEqual(await inner.evaluate(() => window.__reuseViewer.options.dataSource.map((item) => item.image_id)), [ids[1]], "lightbox selection must follow image identity after earlier images are deleted");
    await closeViewer(inner);
    await inner.locator('[data-detail-image="0"]').waitFor();
    await inner.locator(".detail-parameter-row").filter({ hasText: `safe landscape ${marker} image 1` }).first().waitFor();
    await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    assert.equal(await inner.locator(".detail-filmstrip").count(), 0, "returning to detail must refresh the group manifest and its shifted image ordinal");
    await page.screenshot({ path: path.join(output, `${engine}-reuse.png`) });
    await cacheBoundaries(page, inner, manifest.images[1]);
    await invalidDecodeRecovery(page, groupId, manifest.images[1], marker);
    assert.deepEqual(errors, [], "browser exceptions");
    console.log(`${engine} 390: gallery preview reuse, preview-free metadata, pending display/original deduplication, fit/zoom quality reuse, lightbox return, reopen revalidation, browse/preview/display revision invalidation, HTTP/decode retry recovery and bounded media cache passed`);
  } finally {
    originalGate.release(); displayGate.release();
    if (groupId) await api(page, "post", "gallery/delete", { ids: [groupId] });
    await page.close(); await browser.close();
  }
}

(async () => {
  for (const engine of (process.env.STUDIO_ENGINES || "chromium,webkit").split(",")) await run(engine);
  console.log(`Media reuse screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
