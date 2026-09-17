/* Screen-sized media, gesture handover and zoom against the isolated WebUI harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated Image Studio harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-mobile-display-"));
const marker = path.basename(output);
const fixtureScript = String.raw`
import json,random,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(6):
 if index == 5:
  # A real, deterministic high-detail PNG exercises decode and resampling.
  # It intentionally has the reported image dimensions without using user data.
  image=Image.frombytes("L",(2736,1536),random.Random(271828).randbytes(2736*1536)).convert("RGB")
  draw=ImageDraw.Draw(image)
  for x in range(0,2736,32): draw.line((x,0,x,1535),fill=(48,136,188),width=2)
  for y in range(0,1536,48): draw.line((0,y,2735,y),fill=(225,173,52),width=2)
 else:
  image=Image.new("RGB",(900,1200),(70+index*24,170-index*15,185-index*13));draw=ImageDraw.Draw(image)
  draw.rectangle((40,70,850,320),fill=(220,205-index*14,90+index*20))
  draw.polygon([(50,1140),(450,380),(850,1140)],fill=(60+index*20,95,125))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI")
 metadata.add_text("Comment",json.dumps({"prompt":f"{marker} display fixture {index}","model":"display-fixture","steps":24,"seed":index,"width":image.width,"height":image.height,"request_type":"PromptGenerateRequest"}))
 file=folder/f"{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; }
async function api(client, method, endpoint, data) {
  const response = await client[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(client, file, index) {
  const buffer = fs.readFileSync(file);
  const batch = await api(client, "post", "imports/prepare", { items: [{ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(buffer).digest("hex"), overrides: { model: "display-fixture" } }] });
  assert.equal(batch.allowed, true);
  const response = await client.post(`${apiRoot}/${batch.items[0].upload_endpoint}`, { multipart: { file: { name: path.basename(file), mimeType: "image/png", buffer } } });
  assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  const result = await api(client, "post", batch.commit_endpoint, {});
  assert.equal(result.allowed, true); return result.generation_ids[0];
}
async function frames(frame, count = 3) {
  await frame.evaluate(remaining => new Promise(resolve => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick);
  }), count);
}
async function waitUntil(page, check, message) {
  for (let index = 0; index < 100 && !check(); index++) await page.waitForTimeout(25);
  assert.ok(check(), message);
}
async function detailSelected(frame, generation) {
  await frame.waitForFunction(generation => {
    const frame = document.querySelector(".detail-image-frame");
    const image = frame?.querySelector("[data-detail-image]");
    return frame?.dataset.generationId === generation && !frame.dataset.detailSwipeState && image?.complete && image.naturalWidth > 1;
  }, generation);
}
async function detailSwipe(frame, direction = 1) {
  return await frame.evaluate(direction => {
    const frame = document.querySelector(".detail-image-frame"), box = frame.getBoundingClientRect();
    let stamp = performance.now();
    const touch = (type, dx) => {
      const point = { identifier: 43, target: frame, clientX: box.x + box.width / 2 + dx, clientY: box.y + Math.min(140, box.height * .4) };
      const event = new Event(type, { bubbles: true, cancelable: true });
      const points = type === "touchend" ? [] : [point];
      Object.defineProperties(event, { touches: { value: points }, targetTouches: { value: points }, changedTouches: { value: [point] }, timeStamp: { value: stamp += 25 } });
      frame.dispatchEvent(event);
    };
    touch("touchstart", 0); touch("touchmove", -direction * 80); touch("touchmove", -direction * 160);
    const phase = frame.dataset.detailSwipeState;
    touch("touchend", -direction * 160);
    return phase;
  }, direction);
}
async function viewerReady(frame, id, quality = "") {
  await frame.waitForFunction(({ id, quality }) => {
    const viewer = window.__displayViewer, slide = viewer?.currSlide, element = slide?.content.element;
    const source = quality === "display" ? slide?.data.displaySrc : quality === "original" ? slide?.data.originalSrc : "";
    return viewer?.opener.isOpen && slide?.data.image_id === id && element?.naturalWidth > 1
      && element.complete && (!quality || source && element.src === source);
  }, { id, quality });
}
async function viewerState(frame) {
  return await frame.evaluate(() => {
    const viewer = window.__displayViewer, slide = viewer.currSlide, element = slide.content.element;
    const box = element.getBoundingClientRect();
    return { id: slide.data.image_id, index: viewer.currIndex, src: element.src, preview: slide.data.previewSrc,
      display: slide.data.displaySrc || "", original: slide.data.originalSrc || "", initial: slide.zoomLevels.initial,
      secondary: slide.zoomLevels.secondary, max: slide.zoomLevels.max, zoom: slide.currZoomLevel,
      naturalWidth: element.naturalWidth, naturalHeight: element.naturalHeight, width: box.width, height: box.height };
  });
}
async function navigateViewer(frame, id) {
  await frame.evaluate(id => {
    const viewer = window.__displayViewer;
    viewer.goTo(viewer.options.dataSource.findIndex(item => item.image_id === id));
  }, id);
  await viewerReady(frame, id);
}
async function viewerSwipe(page, frame, engine) {
  const size = page.viewportSize(), from = { x: size.width * .78, y: size.height * .45 }, to = { x: size.width * .2, y: from.y };
  if (engine === "chromium") {
    const cdp = await page.context().newCDPSession(page);
    try {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ ...from, id: 1 }] });
      for (let index = 1; index <= 6; index++) {
        await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: from.x + (to.x - from.x) * index / 6, y: from.y, id: 1 }] });
        await frames(frame, 1);
      }
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } finally { await cdp.detach(); }
  } else {
    for (let index = 0; index <= 7; index++) {
      await frame.evaluate(({ from, to, index }) => {
        const target = index === 0 ? window.__displayViewer.currSlide.content.element : window;
        target.dispatchEvent(new PointerEvent(index === 0 ? "pointerdown" : index === 7 ? "pointerup" : "pointermove", {
          bubbles: true, cancelable: true, pointerId: 44, pointerType: "touch", isPrimary: true, button: 0,
          buttons: index === 7 ? 0 : 1, clientX: from.x + (to.x - from.x) * Math.min(index, 6) / 6, clientY: from.y,
        }));
      }, { from, to, index });
      await frames(frame, 1);
    }
  }
}
async function toggleZoom(frame) {
  // Exercise the configured double-tap target, not an arbitrary zoomTo level.
  await frame.evaluate(() => window.__displayViewer.toggleZoom());
}
async function zoomSettled(frame) {
  await frame.waitForFunction(() => !window.__displayViewer.animations.activeAnimations.length);
}
async function showControls(page, frame) {
  await zoomSettled(frame);
  const root = frame.locator(".pswp--open");
  if (!(await root.getAttribute("class")).includes("image-studio-controls-visible")) {
    const point = await frame.evaluate(() => {
      const box = window.__displayViewer.currSlide.content.element.getBoundingClientRect();
      return { x: box.x + box.width / 2, y: box.y + box.height * .3 };
    });
    await page.mouse.click(point.x, point.y);
  }
  await frame.locator(".pswp--open.image-studio-controls-visible").waitFor();
}

async function run(browser, engine, viewport, sequence) {
  const page = await browser.newPage({ viewport, hasTouch: true, deviceScaleFactor: viewport.width <= 540 ? 3 : 2 });
  page.setDefaultTimeout(15000);
  const [large, second, third, displayFailure, originalFailure, restricted] = sequence;
  const errors = [], requests = [], completed = new Set(), waits = new Map(), failures = new Set();
  const requestCount = (id, detail) => requests.filter(item => item.id === id && item.detail === detail).length;
  const key = (id, detail) => `${id}:${detail}`;
  const hold = (id, detail) => { const held = gate(); waits.set(key(id, detail), held); return held; };
  const largeDisplay = hold(large.image_id, "display"), thirdDisplay = hold(third.image_id, "display"), largeOriginal = hold(large.image_id, "original");
  const expandedDisplay = viewport.width <= 540 ? hold(large.image_id, "display:2048") : null;
  page.on("pageerror", error => errors.push(error.message));
  page.on("requestfinished", request => {
    const url = new URL(request.url());
    if (url.pathname.includes("/gallery/image/")) {
      const identity = key(url.pathname.split("/").at(-1), url.searchParams.get("detail"));
      completed.add(identity); completed.add(`${identity}:${url.searchParams.get("max_edge")}`);
    }
  });
  await page.route("**/gallery/image-sequence?*", async route => {
    const response = await route.fetch(), body = await response.json(), data = body.data || body;
    const item = data.items.find(item => item.image_id === restricted.image_id);
    if (item) item.allowed_actions = { ...item.allowed_actions, download: false };
    await route.fulfill({ response, json: body });
  });
  await page.route("**/gallery/image/*", async route => {
    const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1), detail = url.searchParams.get("detail"), identity = key(id, detail);
    requests.push({ id, detail, edge: Number(url.searchParams.get("max_edge")) });
    const pending = waits.get(`${identity}:${url.searchParams.get("max_edge")}`) || waits.get(identity);
    if (pending) await pending.promise;
    try {
      if (failures.has(identity)) await route.fulfill({ status: 503, contentType: "application/json", json: { message: "测试图片暂时不可用" } });
      else await route.continue();
    } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
  });
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.evaluate(() => {
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__displayViewer = this; } };
      window.AstrBotPluginPage.download = async (endpoint, parameters, filename) => { window.__displayDownload = { endpoint, parameters, filename }; };
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      window.__displayImageUrls = []; window.__displayRevokedUrls = []; window.__displayDecodedUrls = [];
      URL.createObjectURL = blob => {
        const url = create(blob);
        if (blob.type.startsWith("image/")) window.__displayImageUrls.push(url);
        return url;
      };
      URL.revokeObjectURL = url => { window.__displayRevokedUrls.push(url); revoke(url); };
      const decode = HTMLImageElement.prototype.decode;
      HTMLImageElement.prototype.decode = function () {
        if (this.src.startsWith("blob:")) window.__displayDecodedUrls.push(this.src);
        return decode.call(this);
      };
      const createScope = window.ImageStudioMediaObjects.createScope;
      window.ImageStudioMediaObjects = {
        createScope() {
          const scope = createScope(), source = scope.source;
          scope.source = async (key, input) => {
            const url = await source(key, input);
            if (window.__displayHoldPreparedKey === key && url) {
              window.__displayHoldPreparedKey = "";
              window.__displayPreparedOriginal = url;
              window.__displayViewer.dispatch("pointerDown", { originalEvent: { pointerId: 95, pointerType: "touch", isPrimary: true } });
            }
            return url;
          };
          return scope;
        },
      };
    });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
    for (const item of sequence) {
      await frame.locator(`[data-gallery-id="${item.generation_id}"]`).scrollIntoViewIfNeeded();
      await frame.waitForFunction(id => {
        const image = document.querySelector(`[data-gallery-id="${id}"] .gallery-image-wrap img`);
        return image?.complete && image.naturalWidth > 0;
      }, item.generation_id);
    }
    await frame.locator(`[data-gallery-id="${large.generation_id}"] .gallery-info`).click();
    await detailSelected(frame, large.generation_id);
    await waitUntil(page, () => requestCount(large.image_id, "display") > 0, "detail must request a bounded display image");
    assert.equal(requests.filter(item => item.detail === "original").length, 0, "fit-sized detail must not download an original");
    if (expandedDisplay) {
      assert.equal(requests.find(item => item.id === large.image_id && item.detail === "display").edge, 1536);
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      largeDisplay.release();
      await waitUntil(page, () => requests.some(item => item.id === large.image_id && item.detail === "display" && item.edge === 2048), "rotating during a pending display request must schedule the larger bucket after the smaller response");
      await page.setViewportSize(viewport); await frames(frame, 3);
      assert.equal(requests.filter(item => item.detail === "original").length, 0);
    }

    assert.equal(await detailSwipe(frame), "dragging");
    await page.waitForTimeout(45);
    assert.equal(await detailSwipe(frame), "dragging", "second detail touch must work while previous settling and display HTTP are pending");
    await detailSelected(frame, third.generation_id);
    await waitUntil(page, () => requestCount(third.image_id, "display") > 0, "new detail group must schedule its display image");
    largeDisplay.release(); expandedDisplay?.release();
    await waitUntil(page, () => completed.has(key(large.image_id, "display")), "release previous display response");
    if (expandedDisplay) await waitUntil(page, () => completed.has(key(large.image_id, "display:2048")), "release the stale landscape display response");
    await frames(frame, 8); await detailSelected(frame, third.generation_id);
    assert.equal(requests.filter(item => item.detail === "original").length, 0);

    await page.waitForTimeout(550); // Detail swipes suppress an accidental click for 500 ms.
    await frame.locator("[data-detail-image]").click(); await viewerReady(frame, third.image_id);
    assert.equal(requestCount(third.image_id, "display"), 1, "detail and viewer must share an in-flight display request");
    thirdDisplay.release(); await viewerReady(frame, third.image_id, "display");
    const sharedDisplay = (await viewerState(frame)).display;
    await frame.evaluate(() => window.__displayViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    await detailSelected(frame, third.generation_id);
    await frame.waitForFunction(src => document.querySelector(".detail-image-frame [data-detail-image]")?.src === src, sharedDisplay);
    await frame.locator("[data-detail-image]").click(); await viewerReady(frame, third.image_id, "display");
    assert.equal((await viewerState(frame)).display, sharedDisplay);
    assert.equal(requestCount(third.image_id, "display"), 1, "reopening must reuse decoded display media without another request");

    // While the large original is pending, zoom back out and immediately page
    // across two groups. Delayed HTTP must never own the active slide identity.
    await navigateViewer(frame, large.image_id); await viewerReady(frame, large.image_id, "display");
    const fit = await viewerState(frame);
    assert.ok(fit.naturalWidth < 2736 && fit.naturalWidth <= 2048);
    assert.ok(Math.abs(fit.secondary - Math.max(fit.initial, Math.min(1, fit.initial * 2.5))) < .0001);
    assert.ok(Math.abs(fit.max - Math.max(fit.initial, Math.min(1, fit.initial * 8))) < .0001);
    assert.equal(requests.filter(item => item.detail === "original").length, 0, "ordinary lightbox browsing only loads display images");
    await toggleZoom(frame); await zoomSettled(frame);
    await waitUntil(page, () => requestCount(large.image_id, "original") > 0, "double-tap zoom must request original pixels");
    assert.ok(Math.abs((await viewerState(frame)).zoom - fit.secondary) < .0001);
    await toggleZoom(frame); await zoomSettled(frame); await viewerReady(frame, large.image_id, "display");
    await viewerSwipe(page, frame, engine);
    await frame.waitForFunction(id => window.__displayViewer.currSlide.data.image_id === id, second.image_id);
    await viewerSwipe(page, frame, engine);
    await viewerReady(frame, third.image_id, "display");
    const beforeLate = await viewerState(frame);
    largeOriginal.release();
    await waitUntil(page, () => completed.has(key(large.image_id, "original")), "release previous original response");
    await frames(frame, 12);
    const afterLate = await viewerState(frame);
    assert.equal(afterLate.id, third.image_id); assert.equal(afterLate.src, beforeLate.src, "stale original must not paint onto the following group");

    // Hold input immediately after the real conversion creates its Blob URL.
    // The following decode/paint jobs must stay queued; abandoning that slide
    // must release the URL even though it was never assigned to originalSrc.
    await navigateViewer(frame, large.image_id); await viewerReady(frame, large.image_id, "display");
    await frame.evaluate(id => { window.__displayHoldPreparedKey = `${id}:original`; }, large.image_id);
    await toggleZoom(frame);
    await frame.waitForFunction(() => !!window.__displayPreparedOriginal);
    await frames(frame, 5);
    const prepared = await frame.evaluate(() => ({
      url: window.__displayPreparedOriginal,
      decoded: window.__displayDecodedUrls.includes(window.__displayPreparedOriginal),
      assigned: !!window.__displayViewer.currSlide.data.originalSrc,
    }));
    assert.match(prepared.url, /^blob:/); assert.equal(prepared.decoded, false); assert.equal(prepared.assigned, false);
    await frame.evaluate(id => {
      const viewer = window.__displayViewer;
      viewer.goTo(viewer.options.dataSource.findIndex(item => item.image_id === id));
      viewer.dispatch("pointerUp", { originalEvent: { pointerId: 95, pointerType: "touch", type: "pointerup" } });
    }, third.image_id);
    await viewerReady(frame, third.image_id, "display");
    await frame.waitForFunction(url => window.__displayRevokedUrls.includes(url), prepared.url);
    assert.equal(await frame.evaluate(url => window.__displayDecodedUrls.includes(url), prepared.url), false, "an abandoned prepared original must be released without decoding it");

    await navigateViewer(frame, large.image_id); await viewerReady(frame, large.image_id, "display");
    await toggleZoom(frame); await viewerReady(frame, large.image_id, "original"); await zoomSettled(frame);
    const zoomed = await viewerState(frame);
    assert.equal(zoomed.naturalWidth, 2736); assert.equal(zoomed.naturalHeight, 1536);
    assert.notEqual(zoomed.original, zoomed.display);
    assert.match(zoomed.original, /^blob:/, "the large original must not remain a base64 image source on the main thread");
    assert.ok(Math.abs(zoomed.zoom - fit.secondary) < .0001, "upgrading pixels must preserve the double-tap zoom level");
    await toggleZoom(frame); await zoomSettled(frame); await viewerReady(frame, large.image_id, "display");
    const restored = await viewerState(frame);
    assert.ok(Math.abs(restored.zoom - fit.initial) < .0001);
    assert.ok(Math.abs(restored.width - fit.width) < 1 && Math.abs(restored.height - fit.height) < 1, "zoom-out must restore the initial display geometry");
    assert.ok(restored.naturalWidth <= 2048, "zoom-out must restore screen-sized pixels rather than retaining the full-resolution raster");
    const originalReads = requestCount(large.image_id, "original");
    await toggleZoom(frame); await viewerReady(frame, large.image_id, "original"); await zoomSettled(frame);
    assert.equal((await viewerState(frame)).original, zoomed.original);
    assert.equal(requestCount(large.image_id, "original"), originalReads, "repeated zoom on the current image must reuse its owned original");
    await toggleZoom(frame); await zoomSettled(frame); await viewerReady(frame, large.image_id, "display");
    await frame.waitForFunction(() => document.querySelector("canvas.image-studio-viewer-backdrop")?.dataset.previewSource === window.__displayViewer.currSlide.data.previewSrc);
    await showControls(page, frame);
    await frame.locator(".pswp__button--image-studio-download").click();
    assert.equal(await frame.evaluate(() => window.__displayDownload.endpoint), `gallery/download/${large.image_id}`, "download must keep its original-file endpoint");

    failures.add(key(displayFailure.image_id, "display"));
    await navigateViewer(frame, displayFailure.image_id);
    await frame.waitForFunction(src => window.__displayRevokedUrls.includes(src), zoomed.original);
    await waitUntil(page, () => requestCount(displayFailure.image_id, "display") > 0, "exercise display failure");
    await frame.waitForFunction(() => !!window.__displayViewer.currSlide.data.displayError);
    const fallback = await viewerState(frame);
    assert.equal(fallback.src, fallback.preview); assert.ok(fallback.naturalWidth > 1, "display failure must keep a usable preview");
    await viewerSwipe(page, frame, engine); await viewerReady(frame, originalFailure.image_id, "display");
    failures.add(key(originalFailure.image_id, "original"));
    await toggleZoom(frame); await zoomSettled(frame);
    await frame.waitForFunction(() => !!window.__displayViewer.currSlide.data.originalError);
    assert.equal((await viewerState(frame)).src, (await viewerState(frame)).display, "original failure must retain the display image");
    await toggleZoom(frame); await zoomSettled(frame);
    await navigateViewer(frame, restricted.image_id); await showControls(page, frame);
    assert.equal(await frame.locator(".pswp__button--image-studio-download").isVisible(), false, "changing quality must not bypass a source's download permission");
    await viewerReady(frame, restricted.image_id, "display");
    await frame.waitForFunction(() => {
      const viewer = window.__displayViewer;
      return viewer.options.dataSource.every((item, index) => Math.abs(index - viewer.currIndex) <= 1 || !item.displaySrc && !item.displayEdge);
    });
    const releasedDisplay = await frame.evaluate(id => {
      const item = window.__displayViewer.options.dataSource.find(item => item.image_id === id);
      return { display: !!item.displaySrc, edge: item.displayEdge || 0, previewOnly: item.src === item.previewSrc };
    }, third.image_id);
    assert.deepEqual(releasedDisplay, { display: false, edge: 0, previewOnly: true }, "far cursors must not retain a second display cache outside the shared byte budget");
    const displayReads = requestCount(third.image_id, "display");
    await navigateViewer(frame, third.image_id); await viewerReady(frame, third.image_id, "display");
    assert.equal((await viewerState(frame)).display, sharedDisplay);
    assert.equal(requestCount(third.image_id, "display"), displayReads, "a pruned cursor must restore its display from the shared cache");
    for (const request of requests.filter(item => item.detail === "display")) assert.ok([768, 1024, 1536, 2048].includes(request.edge), `unbounded display request: ${JSON.stringify(request)}`);
    await page.screenshot({ path: path.join(output, `${engine}-${viewport.width}-display.png`) });
    await frame.evaluate(() => window.__displayViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    await frame.waitForFunction(() => window.__displayImageUrls.every(url => window.__displayRevokedUrls.includes(url)));
    assert.deepEqual(errors, []);
    console.log(`${engine}-${viewport.width}: bounded display media, detail/viewer reuse, consecutive gestures during pending HTTP, stale response isolation, double-tap zoom/out, failure fallbacks and download policy passed`);
  } finally { for (const pending of waits.values()) pending.release(); await page.close(); }
}

(async () => {
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const client = await engines.request.newContext();
  const groups = [];
  try {
    for (let index = 0; index < files.length; index++) groups.push(await seed(client, files[index], index));
    const sequence = (await api(client, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    assert.equal(sequence.length, 6);
    assert.deepEqual(sequence.map(item => item.generation_id), [...groups].reverse());
    assert.equal(sequence[0].width, 2736); assert.equal(sequence[0].height, 1536);
    for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
      if (!["chromium", "webkit"].includes(engine)) throw new Error("STUDIO_BROWSER must be chromium or webkit.");
      const browser = await engines[engine].launch({ headless: true });
      try { for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }]) await run(browser, engine, viewport, sequence); }
      finally { await browser.close(); }
    }
    console.log(`Mobile display artifacts: ${output}`);
  } finally {
    if (groups.length) await api(client, "post", "gallery/delete", { ids: groups });
    await client.dispose();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
