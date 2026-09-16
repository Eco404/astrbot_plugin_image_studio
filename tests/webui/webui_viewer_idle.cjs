/* Run only against the isolated WebUI harness; fixtures are created through its real import API. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const engine = process.env.STUDIO_BROWSER || "chromium";
if (!["chromium", "webkit"].includes(engine)) throw new Error("STUDIO_BROWSER must be chromium or webkit.");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated Image Studio harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-viewer-idle-"));
const marker = path.basename(output);
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(8):
 image=Image.new("RGB",(900,1200),(90+index*15,170-index*9,190-index*7));draw=ImageDraw.Draw(image)
 draw.rectangle((30,40,860,320),fill=(240,220-index*8,130));draw.ellipse((130,420,750,1080),fill=(60,90+index*12,130))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"{marker} fixture image {index}","model":"idle-fixture","steps":20+index,"seed":index,"request_type":"PromptGenerateRequest"}))
 file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

async function unwrap(response) { const body = await response.json(); return body.data || body; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()}`); return await unwrap(response);
}
async function seed(page, files) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "idle-fixture" } }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: true }); assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } }); assert.ok(response.ok());
  }
  return (await api(page, "post", prepared.commit_endpoint, {})).generation_id;
}
async function ready(inner, index, quality = "display") {
  await inner.waitForFunction(({ index, quality }) => {
    const viewer = window.__idleViewer; const item = viewer?.currSlide;
    const source = quality === "original" ? item?.data.originalSrc : item?.data.displaySrc;
    return viewer?.currIndex === index && item?.content.state === "loaded" && item.content.element?.naturalWidth > 1 && !!source && item.content.element.src === source;
  }, { index, quality });
}
async function zoom(inner, multiplier = 2, duration = 0) {
  await inner.evaluate(({ multiplier, duration }) => {
    const viewer = window.__idleViewer;
    viewer.zoomTo(viewer.currSlide.zoomLevels.initial * multiplier, { x: innerWidth / 2, y: innerHeight / 2 }, duration);
  }, { multiplier, duration });
}
async function snapshot(inner) {
  return await inner.evaluate(() => {
    const viewer = window.__idleViewer; const slide = viewer.currSlide;
    return { index: viewer.currIndex, id: slide.data.image_id, generation: slide.data.generation_id, src: slide.content.element?.src, preview: slide.data.previewSrc, display: slide.data.displaySrc || "", original: slide.data.originalSrc || "", zoom: slide.currZoomLevel, pan: { ...slide.pan }, dragging: viewer.gestures.isDragging, shifted: viewer.mainScroll.isShifted(), animations: viewer.animations.activeAnimations.length };
  });
}
async function pointer(inner, type, x, y = 400) {
  await inner.evaluate(({ type, x, y }) => {
    const target = window.__idleViewer.scrollWrap;
    target.dispatchEvent(new PointerEvent(type, { pointerId: 31, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: type === "pointerup" ? 0 : 1, button: 0 }));
  }, { type, x, y });
}
function blockOriginal(page, imageId, responseBody) {
  let release; let received = 0;
  const gate = new Promise((resolve) => { release = resolve; });
  const route = async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.endsWith(`/${imageId}`) || url.searchParams.get("detail") !== "original") return await route.continue();
    received++; await gate;
    try {
      if (responseBody) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: responseBody }) });
      else await route.continue();
    } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
  };
  return { install: () => page.route("**/gallery/image/*", route), received: () => received, release, remove: () => page.unroute("**/gallery/image/*", route) };
}
async function waitRequest(page, gate) {
  for (let index = 0; index < 80 && !gate.received(); index++) await page.waitForTimeout(25);
  assert.ok(gate.received() > 0, "expected an original request to reach the controlled route");
}
async function decoded(inner, imageId) { return await inner.evaluate((id) => window.__idleDecodes.filter((entry) => entry.id === id), imageId); }
async function installProbe(inner, originals) {
  await inner.evaluate((originals) => {
    window.__idleOriginals = new Map(originals); window.__idleBlobOriginals = new Map(); window.__idleDecodes = []; window.__idlePointerEvents = 0;
    // Original display now uses owned Blob URLs. Associate the actual returned
    // URL with the exact original input bytes before the app starts decoding.
    const createScope = window.ImageStudioMediaObjects.createScope;
    window.ImageStudioMediaObjects = {
      createScope() {
        const scope = createScope(), source = scope.source;
        scope.source = async (key, input) => {
          const url = await source(key, input);
          const id = window.__idleOriginals.get(input) || window.__idleBlobOriginals.get(input);
          if (url && id) window.__idleBlobOriginals.set(url, id);
          return url;
        };
        return scope;
      },
    };
    const decode = HTMLImageElement.prototype.decode;
    HTMLImageElement.prototype.decode = function () {
      const id = window.__idleOriginals.get(this.src) || window.__idleBlobOriginals.get(this.src); const viewer = window.__idleViewer;
      if (id) window.__idleDecodes.push({ id, sourceType: this.src.startsWith("blob:") ? "blob" : "data", current: viewer?.currSlide?.data.image_id, dragging: !!viewer?.gestures.isDragging, zooming: !!viewer?.gestures.isZooming, shifted: !!viewer?.mainScroll.isShifted(), animations: viewer?.animations.activeAnimations.length || 0, pointerHeld: !!window.__idlePointerHeld });
      return decode.call(this);
    };
    const Original = window.PhotoSwipe;
    window.PhotoSwipe = class extends Original {
      constructor(options) {
        super(options); window.__idleViewer = this;
        this.on("pointerDown", () => { window.__idlePointerHeld = true; window.__idlePointerEvents++; });
        this.on("pointerUp", () => { window.__idlePointerHeld = false; });
        this.on("afterInit", () => {
          const before = window.__idlePointerEvents;
          window.__idleInitial = { open: this.opener.isOpen, original: !!this.currSlide.data.originalSrc };
          this.scrollWrap.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 31, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: 195, clientY: 400, buttons: 1, button: 0 }));
          this.scrollWrap.dispatchEvent(new PointerEvent("pointerup", { pointerId: 31, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: 195, clientY: 400, buttons: 0, button: 0 }));
          window.__idleInitial.firstPointerAccepted = window.__idlePointerEvents > before;
        });
      }
    };
  }, originals);
}

async function verifyPreparedDetails(page, frame, inner, items) {
  const current = items[7];
  const summary = await api(page, "get", `gallery/detail/${current.generation}?assets=0`);
  const coldPreview = await inner.evaluate(async (src) => {
    const image = new Image(); image.src = src; await image.decode();
    const canvas = document.createElement("canvas"); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    canvas.getContext("2d").drawImage(image, 0, 0); return canvas.toDataURL("image/png");
  }, summary.images[current.imageIndex].thumbnail_data_url);
  const previewRoute = async (route) => {
    const response = await route.fetch(); const body = await response.json(); const data = body.data || body;
    data.images[current.imageIndex].thumbnail_data_url = coldPreview;
    // The viewer already decoded this image's display. A cold-thumbnail probe
    // needs a new immutable identity; otherwise restoring the cached display
    // correctly skips preview decoding entirely.
    data.images[current.imageIndex].sha256 = `cold-preview-${data.images[current.imageIndex].sha256}`;
    await route.fulfill({ response, json: body });
  };
  await page.route(`**/gallery/detail/${current.generation}?*`, previewRoute);
  await inner.evaluate((source) => {
    const decode = HTMLImageElement.prototype.decode; window.__idleRestoreDecode = () => { HTMLImageElement.prototype.decode = decode; };
    window.__idlePrepareHeld = false;
    HTMLImageElement.prototype.decode = async function () {
      if (this.src === source && this.className === "detail-image") { window.__idlePrepareHeld = true; await new Promise((resolve) => { window.__idleReleasePrepare = resolve; }); }
      return await decode.call(this);
    };
  }, coldPreview);
  await pointer(inner, "pointerdown", 190, 300); await pointer(inner, "pointermove", 190, 320); await page.waitForTimeout(25); await pointer(inner, "pointermove", 190, 350);
  await inner.waitForFunction(() => window.__idlePrepareHeld);
  assert.equal(await inner.locator(".detail-image-frame > .detail-image").getAttribute("src"), null);
  await inner.evaluate(() => { window.__idleRestoreDecode(); window.__idleReleasePrepare(); });
  await inner.waitForFunction((source) => document.querySelector(".detail-image-frame > .detail-image")?.src === source, coldPreview);
  assert.equal(await inner.evaluate(() => window.__idleViewer.opener.isOpen && window.__idleViewer.gestures.isDragging), true, "prepared detail preview must paint before the viewer is destroyed");
  await page.waitForTimeout(170); await pointer(inner, "pointerup", 190, 350);
  await inner.waitForFunction(() => !window.__idleViewer.animations.activeAnimations.length);
  assert.equal(await frame.locator(".pswp--open").count(), 1, "a short vertical gesture should cancel, not close the viewer");
  await page.unroute(`**/gallery/detail/${current.generation}?*`, previewRoute);

  let releaseSummary; let requested = 0; const gate = new Promise((resolve) => { releaseSummary = resolve; });
  const staleRoute = async (route) => { requested++; await gate; await route.continue(); };
  await page.route(`**/gallery/detail/${items[1].generation}?*`, staleRoute);
  await inner.evaluate(() => window.__idleViewer.goTo(1)); await ready(inner, 1);
  await pointer(inner, "pointerdown", 190, 300); await pointer(inner, "pointermove", 190, 320); await page.waitForTimeout(25); await pointer(inner, "pointermove", 190, 350);
  for (let index = 0; index < 80 && !requested; index++) await page.waitForTimeout(25);
  assert.ok(requested > 0, "vertical preparation should request the other group's summary");
  await page.waitForTimeout(170); await pointer(inner, "pointerup", 190, 350); await inner.waitForFunction(() => !window.__idleViewer.animations.activeAnimations.length);
  await inner.evaluate(() => window.__idleViewer.goTo(2)); await ready(inner, 2);
  const pendingMarkup = await frame.locator("#drawerBody").innerHTML(); releaseSummary(); await page.waitForTimeout(180);
  assert.equal(await frame.locator("#drawerBody").innerHTML(), pendingMarkup, "a canceled vertical preparation must not repaint an old selection after horizontal navigation");
  await page.unroute(`**/gallery/detail/${items[1].generation}?*`, staleRoute);
  await inner.evaluate(() => window.__idleViewer.goTo(7)); await ready(inner, 7);
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, deviceScaleFactor: 3 });
    page.setDefaultTimeout(15000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
    const groupA = await seed(page, files.slice(0, 4)); const groupB = await seed(page, files.slice(4));
    const rawAssets = [...(await api(page, "get", `gallery/assets/${groupA}`)).images, ...(await api(page, "get", `gallery/assets/${groupB}`)).images];
    const originals = rawAssets.map((item) => [item.data_url, item.id]);
    const inner = page.frames().find((item) => item.url().includes("/ui/")); await installProbe(inner, originals);
    await frame.locator('[data-view="gallery"]').click(); await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab"); await frame.locator(`[data-gallery-id="${groupB}"]`).waitFor();
    await frame.locator(`[data-gallery-id="${groupB}"] .gallery-info`).click(); await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    await frame.locator("[data-detail-image]").click();
    await inner.waitForFunction(() => !!window.__idleInitial);
    const initial = await inner.evaluate(() => window.__idleInitial);
    assert.equal(initial.open, true, "the viewer should accept gestures as soon as afterInit runs");
    assert.equal(initial.firstPointerAccepted, true, "the first pointer must not be swallowed by an opening animation");
    assert.equal(initial.original, false, "fit viewing must not eagerly fetch originals");
    await ready(inner, 0);
    assert.equal((await snapshot(inner)).original, "");
    const items = await inner.evaluate(() => window.__idleViewer.options.dataSource.map((item) => ({ id: item.image_id, generation: item.generation_id, imageIndex: item.image_index })));
    assert.equal(items.length, 8, "search must isolate this test's two four-image groups");
    assert.notEqual(items[0].generation, items[4].generation);
    await page.waitForTimeout(400);
    const blockedDetailRequests = [];
    const requestObserver = (request) => { if (/\/gallery\/(detail|assets)\//.test(request.url())) blockedDetailRequests.push(request.url()); };
    page.on("request", requestObserver);
    await inner.evaluate(() => {
      window.__idleDrawerChanges = [];
      window.__idleDrawerObserver = new MutationObserver((records) => { window.__idleDrawerChanges.push(...records.map((record) => ({ type: record.type, target: record.target.nodeName }))); });
      window.__idleDrawerObserver.observe(document.querySelector("#drawerBody"), { childList: true, subtree: true, characterData: true });
      window.__idleBackground = document.querySelector(".image-studio-viewer-background");
    });

    const held = blockOriginal(page, items[4].id); await held.install();
    await inner.evaluate(() => window.__idleViewer.goTo(4)); await ready(inner, 4); await zoom(inner); await waitRequest(page, held);
    await pointer(inner, "pointerdown", 285); held.release(); await page.waitForTimeout(140);
    assert.deepEqual(await decoded(inner, items[4].id), [], "an original decoded while the pointer remained pressed");
    assert.equal((await snapshot(inner)).original, "");
    await pointer(inner, "pointermove", 270); await page.waitForTimeout(30); await pointer(inner, "pointermove", 235); await page.waitForTimeout(40);
    assert.equal((await snapshot(inner)).dragging, true, "the probe must exercise PhotoSwipe's actual drag handler");
    assert.deepEqual(await decoded(inner, items[4].id), [], "an original decoded during drag");
    await page.waitForTimeout(170); await pointer(inner, "pointerup", 235); await ready(inner, 4, "original"); await held.remove();
    const dragDecodes = await decoded(inner, items[4].id); assert.ok(dragDecodes.length > 0);
    assert.ok(dragDecodes.every(item => item.sourceType === "blob"), "the original decode probe must follow the owned Blob URL");
    assert.ok(dragDecodes.every((item) => !item.pointerHeld && !item.dragging && !item.zooming && !item.shifted && !item.animations), JSON.stringify(dragDecodes));

    const zoomGate = blockOriginal(page, items[5].id); await zoomGate.install();
    await inner.evaluate(() => window.__idleViewer.goTo(5)); await ready(inner, 5); await zoom(inner, 1.5); await waitRequest(page, zoomGate);
    await zoom(inner, 2, 400);
    const zoomed = await snapshot(inner); assert.ok(zoomed.animations > 0, "zoom transition should be active"); zoomGate.release(); await page.waitForTimeout(90);
    assert.deepEqual(await decoded(inner, items[5].id), [], "an original decoded during zoom animation");
    await ready(inner, 5, "original"); const upgraded = await snapshot(inner); assert.ok(Math.abs(upgraded.zoom - zoomed.zoom) < .001); assert.deepEqual(upgraded.pan, zoomed.pan); await zoomGate.remove();
    await inner.evaluate(() => window.__idleViewer.zoomTo(window.__idleViewer.currSlide.zoomLevels.initial, undefined, 0));

    const stale = blockOriginal(page, items[6].id); await stale.install();
    await inner.evaluate(() => window.__idleViewer.goTo(6)); await ready(inner, 6); await zoom(inner); await waitRequest(page, stale);
    await inner.evaluate(() => window.__idleViewer.goTo(7)); await ready(inner, 7); stale.release(); await page.waitForTimeout(200);
    assert.deepEqual(await decoded(inner, items[6].id), [], "a stale non-current original should not be decoded after a quick switch"); await stale.remove();
    assert.equal((await snapshot(inner)).id, items[7].id);
    const background = await inner.evaluate(() => {
      const holder = document.querySelector(".image-studio-viewer-background"); const image = holder?.querySelector("canvas.image-studio-viewer-backdrop");
      return { same: holder === window.__idleBackground, filter: holder && getComputedStyle(holder).filter, imageFilter: image && getComputedStyle(image).filter, src: image?.dataset.previewSource, preview: window.__idleViewer.currSlide.data.previewSrc, width: image?.width, height: image?.height };
    });
    await inner.waitForFunction(() => document.querySelector("canvas.image-studio-viewer-backdrop")?.dataset.previewSource === window.__idleViewer.currSlide.data.previewSrc);
    assert.equal(background.same, true); assert.equal(background.filter, "none"); assert.equal(background.imageFilter, "none");
    assert.ok(background.width > 1 && background.height > 1 && Math.max(background.width, background.height) <= 320);
    assert.deepEqual(blockedDetailRequests, [], "the obscured detail modal must not load records/assets during viewer navigation");
    assert.deepEqual(await inner.evaluate(() => window.__idleDrawerChanges), [], "the obscured detail modal must not be rerendered during viewer navigation");
    await page.screenshot({ path: path.join(output, `${engine}-idle-final.png`) });
    page.off("request", requestObserver); await inner.evaluate(() => window.__idleDrawerObserver.disconnect());
    await verifyPreparedDetails(page, frame, inner, items);
    await inner.evaluate(() => window.__idleViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    await frame.locator(`.detail-filmstrip[data-generation-id="${items[7].generation}"] [data-detail-dot="${items[7].imageIndex}"][aria-current="true"]`).waitFor();
    await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    await frame.locator("[data-detail-image]").click(); await inner.waitForFunction(() => window.__idleViewer?.opener.isOpen); await ready(inner, 7);
    assert.equal((await snapshot(inner)).id, items[7].id, "reopening must preserve the image selected in the viewer");

    const failure = blockOriginal(page, items[1].id, "测试原图暂时不可用"); await failure.install();
    await inner.evaluate(() => window.__idleViewer.goTo(1)); await ready(inner, 1); await zoom(inner); await waitRequest(page, failure); failure.release();
    await frame.locator(".image-studio-image-status:not([hidden])").waitFor(); assert.match(await frame.locator(".image-studio-image-status").innerText(), /高清图片加载失败|图片暂时无法加载/);
    await page.screenshot({ path: path.join(output, `${engine}-idle-retry.png`) }); await failure.remove();
    await zoom(inner, 1); await ready(inner, 1);
    await pointer(inner, "pointerdown", 190, 300);
    for (const y of [320, 375, 440, 510, 590, 660]) { await pointer(inner, "pointermove", 190, y); await page.waitForTimeout(25); }
    await pointer(inner, "pointerup", 190, 660); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    await frame.locator(`.detail-filmstrip[data-generation-id="${items[1].generation}"] [data-detail-dot="${items[1].imageIndex}"][aria-current="true"]`).waitFor();
    await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    await frame.locator("[data-detail-image]").click(); await inner.waitForFunction(() => window.__idleViewer?.opener.isOpen); await ready(inner, 1);
    await zoom(inner); await ready(inner, 1, "original");
    let repeatedOriginals = 0;
    const observeCachedOriginal = request => {
      const url = new URL(request.url());
      if (url.pathname.endsWith(`/${items[6].id}`) && url.searchParams.get("detail") === "original") repeatedOriginals++;
    };
    page.on("request", observeCachedOriginal);
    await inner.evaluate(() => window.__idleViewer.goTo(6)); await ready(inner, 6); await zoom(inner); await ready(inner, 6, "original"); await page.waitForTimeout(160);
    page.off("request", observeCachedOriginal);
    assert.equal(repeatedOriginals, 0, "a downloaded stale original must be reusable by a later viewer without another request");
    // Image 6's earlier stale response is reusable in the shared media cache.
    // Use an original that has never been requested for the pending-close case.
    const closingIndex = 3;
    const closing = blockOriginal(page, items[closingIndex].id); await closing.install();
    await inner.evaluate(index => window.__idleViewer.goTo(index), closingIndex); await ready(inner, closingIndex); await zoom(inner); await waitRequest(page, closing);
    await inner.evaluate(() => { window.__idleClosingViewer = window.__idleViewer; window.__idleViewer.close(); }); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    closing.release(); await page.waitForTimeout(180); await closing.remove();
    assert.equal(await frame.locator(".pswp--open").count(), 0, "a late original response must not revive the closed viewer");
    assert.equal(await inner.evaluate(index => window.__idleClosingViewer.options.dataSource[index].originalSrc || "", closingIndex), "", "a late original response must not upgrade a closed session");
    assert.deepEqual(errors, []); await page.close();
    console.log(`${engine}: pointer/drag/settle/zoom idle gating, stale-original suppression, deferred details, thumbnail-only fixed backdrop, Chinese recovery and close synchronization passed`);
    console.log(`Viewer idle screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
