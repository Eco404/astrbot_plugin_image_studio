/* Cold/failed media must never turn a navigable mobile slide into a false error
 * or PhotoSwipe's opaque placeholder. Use only the isolated WebUI harness.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const engine = process.env.STUDIO_BROWSER || "chromium";
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated Image Studio harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-viewer-loading-"));
const marker = path.basename(output);
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(12):
 image=Image.new("RGB",(280,360),(90+index*12,170-index*7,190-index*5));draw=ImageDraw.Draw(image)
 draw.rectangle((30,40,250,120),fill=(240,220-index*8,130));draw.ellipse((40,160,240,320),fill=(60,90+index*12,130))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"{marker} image {index}","model":"loading-fixture","seed":index,"request_type":"PromptGenerateRequest"}))
 file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release, calls: 0, fail: false };
}
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(page) {
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex") }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: false }); assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok());
  }
  const committed = await api(page, "post", prepared.commit_endpoint, {}); assert.equal(committed.allowed, true);
  return (await api(page, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
}
async function loaded(inner, index, original = true) {
  await inner.waitForFunction(({ index, original }) => {
    const viewer = window.__loadingViewer, slide = viewer?.currSlide;
    return viewer?.currIndex === index && slide?.content.state === "loaded" && slide.content.element?.naturalWidth > 1 && (!original || !!slide.data.originalSrc);
  }, { index, original });
}
async function settle(inner, index) {
  await inner.waitForFunction(index => {
    const viewer = window.__loadingViewer;
    return viewer?.currIndex === index && !viewer.gestures.isDragging && !viewer.mainScroll.isShifted() && !viewer.animations.activeAnimations.length;
  }, index, { timeout: 2000 });
}
async function swipe(inner, direction = 1) {
  const index = await inner.evaluate(() => window.__loadingViewer.currIndex);
  const from = direction > 0 ? 320 : 70, to = direction > 0 ? 70 : 320;
  for (let step = 0; step <= 7; step++) {
    const type = step === 0 ? "pointerdown" : step === 7 ? "pointerup" : "pointermove";
    await inner.evaluate(({ type, x }) => window.__loadingViewer.scrollWrap.dispatchEvent(new PointerEvent(type, { pointerId: 43, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: 400, buttons: type === "pointerup" ? 0 : 1, button: 0 })), { type, x: from + (to - from) * Math.min(step, 6) / 6 });
    if (step && step < 7) await inner.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }
  try { await settle(inner, index + direction); }
  catch (error) {
    error.message += `\nSwipe snapshot: ${JSON.stringify(await inner.evaluate(() => {
      const viewer = window.__loadingViewer;
      return { index: viewer.currIndex, shifted: viewer.mainScroll.isShifted(), dragging: viewer.gestures.isDragging, dragAxis: viewer.gestures.dragAxis, touch: viewer.gestures.supportsTouch, pointer: viewer.gestures.supportsPointerEvents, scroll: viewer.mainScroll.x, animations: viewer.animations.activeAnimations.map(item => item.props) };
    }))}`;
    throw error;
  }
}
async function assertLoading(inner, label, milliseconds = 220) {
  const samples = await inner.evaluate(milliseconds => new Promise(resolve => {
    const samples = [], end = performance.now() + milliseconds;
    const visible = element => {
      for (let node = element; node instanceof Element; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
      }
      return !!element?.getClientRects().length;
    };
    const tick = () => {
      const viewer = window.__loadingViewer, slide = viewer.currSlide;
      const placeholders = [...slide.container.querySelectorAll(".pswp__img--placeholder, .pswp__img--with-bg")];
      const opaque = placeholders.filter(visible).map(element => getComputedStyle(element).backgroundColor).filter(color => color !== "transparent" && !/rgba\([^)]*,\s*0\)$/.test(color));
      const errors = [...document.querySelectorAll(".pswp__error-msg, .image-studio-image-status")].filter(visible).map(element => element.textContent).filter(text => /无法加载|加载失败|重新加载/.test(text));
      let pixelAlpha = 0;
      const image = slide.content.element;
      if (image?.tagName === "IMG" && image.complete && image.naturalWidth === 1) {
        const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d"); context.drawImage(image, 0, 0); pixelAlpha = context.getImageData(0, 0, 1, 1).data[3];
      }
      samples.push({ index: viewer.currIndex, opaque, errors, pixelAlpha });
      if (performance.now() >= end) resolve(samples); else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }), milliseconds);
  assert.ok(samples.length > 1);
  assert.deepEqual(samples.filter(sample => sample.opaque.length || sample.errors.length || sample.pixelAlpha), [], `${label}: pending frames displayed a false failure or opaque placeholder`);
}
async function waitCalls(page, plan) {
  for (let retry = 0; retry < 100 && !(plan.preview.calls && plan.original.calls); retry++) await page.waitForTimeout(20);
  assert.ok(plan.preview.calls && plan.original.calls, `both preview and original should be requested: ${JSON.stringify({ preview: plan.preview.calls, original: plan.original.calls })}`);
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, deviceScaleFactor: 3 });
    page.setDefaultTimeout(15000); const errors = []; page.on("pageerror", error => errors.push(error.message));
    const items = await seed(page); assert.equal(items.length, 12);
    // Remove gallery-carried thumbnails for distant records to exercise a real
    // cold cross-group slide without mutating the viewer's cache internals.
    await page.route("**/gallery/list?*", async route => {
      const response = await route.fetch(); const body = await response.json(), payload = body.data || body;
      for (const item of payload.items || []) if (item.id !== items[0].generation_id) item.thumbnail_data_url = "";
      await route.fulfill({ response, json: body });
    });
    const plans = new Map();
    for (const index of [4, 5, 7, 9, 11]) plans.set(items[index].image_id, { preview: gate(), original: gate() });
    const mediaRoute = async route => {
      const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1), kind = url.searchParams.get("detail");
      const branch = plans.get(id)?.[kind];
      if (branch) {
        branch.calls++; await branch.promise;
        if (branch.fail) { await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试媒体暂时不可用" }) }); return; }
      }
      await route.continue();
    };
    await page.route("**/gallery/image/*", mediaRoute);
    await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    const inner = page.frames().find(item => item.url().includes("/ui/"));
    await inner.evaluate(() => { const Original = window.PhotoSwipe; window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__loadingViewer = this; } }; });
    await frame.locator('[data-view="gallery"]').click(); await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
    await frame.locator(`[data-gallery-id="${items[0].generation_id}"] .gallery-info`).click(); await frame.locator("#detailUseReference:not(:disabled)").waitFor(); await frame.locator("[data-detail-image]").click();
    await loaded(inner, 0); assert.equal(await inner.evaluate(() => window.__loadingViewer.options.dataSource.length), 12);

    await inner.evaluate(() => window.__loadingViewer.goTo(4)); await settle(inner, 4); await waitCalls(page, plans.get(items[4].image_id));
    assert.equal(await inner.evaluate(() => !!window.__loadingViewer.currSlide.data.previewSrc || !!window.__loadingViewer.currSlide.data.originalSrc), false, "cold fixture unexpectedly had cached image content");
    await assertLoading(inner, "both network requests pending");
    await page.screenshot({ path: path.join(output, `${engine}-network-loading.png`) });
    const transientError = await inner.evaluate(() => {
      const content = window.__loadingViewer.currSlide.content;
      content.onError(); return { state: content.state, message: content.element?.textContent };
    });
    assert.equal(transientError.state, "error", "the cold slide must exercise PhotoSwipe's own error path");
    assert.equal(transientError.message, "", "a temporary native image error must defer to the unified loading status");
    await assertLoading(inner, "temporary PhotoSwipe error while real image sources remain pending");
    await swipe(inner); await assertLoading(inner, "continued cross-group swipe before image load"); await swipe(inner, -1);
    for (const index of [4, 5]) { const plan = plans.get(items[index].image_id); plan.preview.release(); plan.original.release(); }
    await loaded(inner, 4);

    // Preview failure must not report a full failure while the original runs.
    const previewFailure = plans.get(items[7].image_id); previewFailure.preview.fail = true; previewFailure.preview.release();
    await inner.evaluate(() => window.__loadingViewer.goTo(7)); await settle(inner, 7); await waitCalls(page, previewFailure); await assertLoading(inner, "failed preview with original pending");
    previewFailure.original.release(); await loaded(inner, 7);

    // Original failure plus scheduled retry is still loading while preview runs.
    const originalFailure = plans.get(items[9].image_id); originalFailure.original.fail = true; originalFailure.original.release();
    await inner.evaluate(() => window.__loadingViewer.goTo(9)); await settle(inner, 9); await waitCalls(page, originalFailure); await assertLoading(inner, "failed original with preview pending", 850);
    originalFailure.original.fail = false; originalFailure.preview.release(); await loaded(inner, 9, false);
    await frame.locator(".image-studio-image-status:not([hidden]) button:not(:disabled)").click(); await loaded(inner, 9);

    // Decode is independent of network completion: not-yet-decoded images must
    // keep the same loading UI and still accept the next actual touch gesture.
    const decodePlan = plans.get(items[11].image_id);
    const decodeSources = await Promise.all(["preview", "original"].map(async detail => (await api(page, "get", `gallery/image/${items[11].image_id}?detail=${detail}`)).data_url));
    await inner.evaluate(sources => {
      const decode = HTMLImageElement.prototype.decode; window.__loadingHeldDecodes = 0;
      const gate = new Promise(resolve => { window.__loadingReleaseDecode = resolve; });
      HTMLImageElement.prototype.decode = function () {
        if (sources.includes(this.src)) { window.__loadingHeldDecodes++; return gate.then(() => decode.call(this)); }
        return decode.call(this);
      };
      window.__loadingRestoreDecode = () => { HTMLImageElement.prototype.decode = decode; };
    }, decodeSources);
    decodePlan.preview.release(); decodePlan.original.release(); await inner.evaluate(() => window.__loadingViewer.goTo(11)); await settle(inner, 11);
    await inner.waitForFunction(() => window.__loadingHeldDecodes > 0); await assertLoading(inner, "decode pending");
    await swipe(inner, -1); await inner.evaluate(() => { window.__loadingRestoreDecode(); window.__loadingReleaseDecode(); }); await loaded(inner, 10);
    await inner.evaluate(() => window.__loadingViewer.goTo(11)); await loaded(inner, 11);

    // Reopen a fresh browser context for true total failure; successful caches
    // from the cases above must not conceal either failed endpoint.
    await page.close();
    const failedPage = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true }); failedPage.setDefaultTimeout(15000);
    let failing = true;
    await failedPage.route("**/gallery/list?*", async route => {
      const response = await route.fetch(); const body = await response.json(), payload = body.data || body;
      for (const item of payload.items || []) if (item.id !== items[0].generation_id) item.thumbnail_data_url = "";
      await route.fulfill({ response, json: body });
    });
    await failedPage.route("**/gallery/image/*", async route => {
      if (failing && new URL(route.request().url()).pathname.endsWith(`/${items[7].image_id}`)) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试媒体不可用" }) });
      else await route.continue();
    });
    await failedPage.goto(base); const failedFrame = failedPage.frameLocator("#studio"); await failedFrame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    const failedInner = failedPage.frames().find(item => item.url().includes("/ui/"));
    await failedInner.evaluate(() => { const Original = window.PhotoSwipe; window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__loadingViewer = this; } }; });
    await failedFrame.locator('[data-view="gallery"]').click(); await failedFrame.locator("#gallerySearch").fill(marker); await failedFrame.locator("#gallerySearch").press("Tab");
    await failedFrame.locator(`[data-gallery-id="${items[0].generation_id}"] .gallery-info`).click(); await failedFrame.locator("#detailUseReference:not(:disabled)").waitFor(); await failedFrame.locator("[data-detail-image]").click(); await loaded(failedInner, 0);
    await failedInner.evaluate(() => window.__loadingViewer.goTo(7));
    const retry = failedFrame.locator(".image-studio-image-status:not([hidden]) button:not(:disabled)"); await retry.waitFor();
    assert.match(await failedFrame.locator(".image-studio-image-status").innerText(), /图片暂时无法加载/);
    await failedPage.screenshot({ path: path.join(output, `${engine}-actual-failure.png`) });
    failing = false; await retry.click(); await loaded(failedInner, 7); await failedFrame.locator(".image-studio-image-status").waitFor({ state: "hidden" });
    assert.deepEqual(errors, []); await failedPage.close();
    console.log(`${engine}: cold cross-group touch navigation, transparent placeholder, both partial failure orders, delayed decode, total failure and retry passed. Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
