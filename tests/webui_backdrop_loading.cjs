/* Decode-order regression checks for mobile detail and fullscreen backgrounds. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const browserName = process.env.STUDIO_BROWSER || "chromium";
if (!["chromium", "webkit"].includes(browserName)) throw new Error("STUDIO_BROWSER must be chromium or webkit.");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated Image Studio WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-backdrop-loading-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(4):
 image=Image.new("RGB",(560,760),[(156,206,218),(214,168,180),(187,211,156),(201,180,218)][index]);draw=ImageDraw.Draw(image)
 draw.rectangle((0,490,560,760),fill=(95+index*18,145,124));draw.polygon([(0,520),(240,160),(520,520)],fill=(110,139+index*10,161));draw.ellipse((400,85,480,165),fill=(238,234,216))
 info=PngImagePlugin.PngInfo();info.add_text("Software","NovelAI");info.add_text("Comment",json.dumps({"prompt":f"safe background {marker} frame {index}","model":"background-model","uc":"blur","steps":20+index,"seed":index,"width":560,"height":760,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=info);paths.append(str(file))
print(json.dumps(paths))
`;

async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const value = await response.json(); return value.data || value;
}

async function seed(page, files, marker) {
  const batch = await api(page, "post", "imports/prepare", { as_group: true, items: files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "background-model" } })) });
  assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  }
  const result = await api(page, "post", batch.commit_endpoint, {}); assert.equal(result.allowed, true); return result.generation_ids[0];
}

async function installDecodeGates(inner) {
  await inner.evaluate(() => {
    const nativeDecode = HTMLImageElement.prototype.decode;
    window.__backdropGates = new Map();
    HTMLImageElement.prototype.decode = function () {
      const gate = /backdrop/.test(this.className || "") ? window.__backdropGates.get(this.src) : null;
      if (!gate) return nativeDecode.call(this);
      gate.calls++;
      return gate.promise.then(() => nativeDecode.call(this));
    };
    window.__armBackdrop = (src) => {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      promise.catch(() => {});
      window.__backdropGates.set(src, { promise, resolve, reject, calls: 0 });
    };
    window.__releaseBackdrop = (src, failure = false) => {
      const gate = window.__backdropGates.get(src);
      if (!gate) return;
      if (!failure) window.__backdropGates.delete(src);
      failure ? gate.reject(new DOMException("Synthetic background decode failure", "EncodingError")) : gate.resolve();
    };
  });
}

async function arm(inner, src) { await inner.evaluate((value) => window.__armBackdrop(value), src); }
async function release(inner, src, failure = false) { await inner.evaluate(({ src, failure }) => window.__releaseBackdrop(src, failure), { src, failure }); }

async function snapshot(inner, selector) {
  return await inner.evaluate((selector) => Array.from(document.querySelectorAll(selector)).map((image) => ({ src: image instanceof HTMLCanvasElement ? image.dataset.previewSource : image.src, opacity: Number(getComputedStyle(image).opacity), loaded: image instanceof HTMLCanvasElement ? image.width > 1 && image.height > 1 && image.getContext("2d").getImageData(0, 0, 1, 1).data[3] === 255 : image.complete && image.naturalWidth > 1, className: image.className, connected: image.isConnected })), selector);
}

async function retained(inner, selector, previous, label) {
  const layers = await snapshot(inner, selector);
  assert.ok(layers.some((layer) => layer.src === previous && layer.loaded && layer.opacity > .3), `${label}: old decoded background must remain visible ${JSON.stringify(layers.map((layer) => ({ oldSource: layer.src === previous, sourceLength: layer.src.length, opacity: layer.opacity, loaded: layer.loaded, className: layer.className })))}`);
}

async function waitVisible(inner, selector, source) {
  await inner.waitForFunction(({ selector, source }) => Array.from(document.querySelectorAll(selector)).some((image) => !image.className.includes("previous") && (image instanceof HTMLCanvasElement ? image.dataset.previewSource === source && image.width > 1 && image.height > 1 : image.src === source && image.complete && image.naturalWidth > 1) && Number(getComputedStyle(image).opacity) > .6 && image.dataset.backdropState !== "fading" && image.dataset.backdropState !== "loading"), { selector, source });
}

async function intermediateFade(page, inner, selector, target, name, opacity) {
  if (await inner.locator(selector).first().evaluate(image => image instanceof HTMLCanvasElement)) {
    const sample = await inner.waitForFunction(({ selector, target }) => {
      const canvas = document.querySelector(selector);
      const progress = Number(canvas?.dataset.fadeProgress);
      return canvas?.dataset.backdropState === "fading" && canvas.dataset.pendingSource === target && progress > 0 && progress < 1 ? { pixels: canvas.toDataURL(), opacity: Number(getComputedStyle(canvas).opacity) } : false;
    }, { selector, target }, { polling: "raf", timeout: 3500 });
    const during = await sample.jsonValue(); await sample.dispose();
    assert.equal(during.opacity, 1, "the fullscreen canvas must remain opaque during its internal blend");
    await page.screenshot({ path: path.join(output, `${name}-fading.png`) });
    await waitVisible(inner, selector, target);
    assert.notEqual(await inner.locator(selector).evaluate(canvas => canvas.toDataURL()), during.pixels, "the fullscreen transition must paint intermediate pixels, not only update its state label");
    assert.equal(await inner.locator(selector).count(), 1, "fullscreen keeps one mounted background canvas");
    return;
  }
  await inner.waitForFunction(({ selector, target, opacity }) => Array.from(document.querySelectorAll(selector)).some((image) => image.src === target && Number(getComputedStyle(image).opacity) > .01 && Number(getComputedStyle(image).opacity) < opacity - .025), { selector, target, opacity }, { polling: "raf", timeout: 3500 });
  await page.screenshot({ path: path.join(output, `${name}-fading.png`) });
  await waitVisible(inner, selector, target);
}

async function clickDetailThumb(page, inner, index) {
  const button = inner.locator(`[data-detail-dot="${index}"]`);
  const rect = await button.boundingBox();
  assert.ok(rect && rect.y >= 0 && rect.y + rect.height <= page.viewportSize().height, "detail thumbnail must be reachable without scrolling the main image away");
  await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2);
  await inner.waitForFunction((index) => document.querySelector("[data-detail-image]")?.dataset.detailImage === String(index), index);
  const main = await inner.locator(".detail-image-frame").boundingBox();
  assert.ok(main && main.y >= 0 && main.y + main.height <= page.viewportSize().height - 40, "main image must remain fully visible after thumbnail selection");
}

async function exerciseSurface(page, inner, surface, test) {
  const { selector, sources, navigate, opacity } = surface;
  const alternatives = (index) => Array.from(new Set([sources[index], ...(surface.alternatives?.[index] || [])])).filter(Boolean);
  const armImage = async (index) => { for (const source of alternatives(index)) await arm(inner, source); };
  const releaseImage = async (index, failure = false) => { for (const source of alternatives(index)) await release(inner, source, failure); };
  const waitGate = async (index) => inner.waitForFunction((values) => values.some((src) => window.__backdropGates.get(src)?.calls > 0), alternatives(index));
  const actualSource = async (index) => surface.sourceFor ? await surface.sourceFor(index) : sources[index];
  await navigate(0); const source0 = await actualSource(0); await waitVisible(inner, selector, source0);
  await armImage(1); await navigate(1);
  await waitGate(1); const source1 = await actualSource(1);
  await page.waitForTimeout(620);
  await retained(inner, selector, source0, `${surface.name}: decode delayed 620ms`);
  await page.screenshot({ path: path.join(output, `${test.name}-${surface.name}-waiting.png`) });
  await releaseImage(1);
  await intermediateFade(page, inner, selector, source1, `${test.name}-${surface.name}`, opacity);

  await armImage(2); await navigate(2);
  await waitGate(2);
  await releaseImage(2, true); await page.waitForTimeout(360);
  await retained(inner, selector, source1, `${surface.name}: failed decode`);

  await navigate(0); await waitVisible(inner, selector, await actualSource(0));
  await armImage(2); await navigate(2);
  await waitGate(2);
  await armImage(3); await navigate(3);
  await waitGate(3); const source3 = await actualSource(3);
  await releaseImage(3); await waitVisible(inner, selector, source3);
  await releaseImage(2); await page.waitForTimeout(380);
  await retained(inner, selector, source3, `${surface.name}: stale decode must not replace newest image`);
  const obsoleteVisible = (await snapshot(inner, selector)).some((layer) => alternatives(2).includes(layer.src) && layer.opacity > .03);
  assert.equal(obsoleteVisible, false, `${surface.name}: stale background became visible`);
  console.log(`${test.name}/${surface.name}: delayed decode, visible intermediate transition, failed decode and stale result exclusion passed`);
}

async function fullscreenMissingAndClose(page, inner, frame, surface) {
  const { sources, selector } = surface;
  await surface.navigate(0); await waitVisible(inner, selector, sources[0]);
  let releaseNetwork; const networkGate = new Promise((resolve) => { releaseNetwork = resolve; });
  const routePattern = "**/gallery/image/*";
  const delay = async (route) => { await networkGate; try { await route.continue(); } catch (error) { if (!/already handled|closed|disposed/i.test(error.message)) throw error; } };
  await page.route(routePattern, delay);
  try {
    await inner.evaluate(() => {
      const viewer = window.__testViewer;
      const item = viewer.options.dataSource[3];
      item.previewSrc = ""; item.originalSrc = ""; item.src = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
      item.loadedDetail = "";
      viewer.goTo(3);
    });
    await page.waitForTimeout(170);
    await retained(inner, selector, sources[0], "missing fullscreen preview must retain old backdrop");
    await inner.evaluate(() => window.__testViewer.close());
    await frame.locator(".pswp--open").waitFor({ state: "detached" });
    releaseNetwork(); await page.waitForTimeout(450);
    assert.equal(await inner.locator(".image-studio-viewer-backdrop").count(), 0, "late image results must not recreate a closed fullscreen backdrop");
    assert.equal(await inner.locator(".pswp--open").count(), 0);
  } finally { releaseNetwork(); await page.unroute(routePattern, delay); }
}

async function pendingDecodeDuringDrag(page, inner, surface, test) {
  const { selector, sources } = surface;
  await surface.navigate(0); await waitVisible(inner, selector, sources[0]);
  const candidates = Array.from(new Set([sources[1], ...(surface.alternatives?.[1] || [])]));
  for (const source of candidates) await arm(inner, source);
  await surface.navigate(1);
  await inner.waitForFunction((source) => window.__backdropGates.get(source)?.calls > 0, sources[1]);
  await retained(inner, selector, sources[0], "before drag with pending decode");
  const previous = await snapshot(inner, selector);
  await inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame");
    const rect = frame.getBoundingClientRect();
    const start = { identifier: 0, clientX: rect.left + rect.width * .7, clientY: rect.top + rect.height * .5 };
    const dispatch = (type, points, changed) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, { touches: { value: points }, changedTouches: { value: changed } });
      frame.dispatchEvent(event);
    };
    dispatch("touchstart", [start], [start]);
    const moved = { ...start, clientX: start.clientX - 26 };
    dispatch("touchmove", [moved], [moved]);
    window.__finishBackdropDrag = () => dispatch("touchend", [], [moved]);
  });
  await inner.waitForFunction(() => document.querySelector(".detail-image-frame")?.dataset.detailSwipeState === "dragging");
  for (const source of candidates) await release(inner, source);
  await page.waitForTimeout(620);
  const held = await snapshot(inner, selector);
  assert.deepEqual(held, previous, "a decode begun before dragging must not change background src, layers or opacity while dragging");
  assert.equal(await inner.locator("[data-detail-image]").getAttribute("data-detail-image"), "1");
  await page.screenshot({ path: path.join(output, `${test.name}-detail-pending-decode-drag.png`) });
  await inner.evaluate(() => window.__finishBackdropDrag());
  await inner.locator(".detail-swipe-overlay").waitFor({ state: "detached" });
  await intermediateFade(page, inner, selector, sources[1], `${test.name}-detail-rollback-resume`, surface.opacity);
  assert.equal(await inner.locator("[data-detail-image]").getAttribute("data-detail-image"), "1", "short drag should roll back without changing selection");
  console.log(`${test.name}/detail: pre-existing decode freezes throughout drag and resumes fading only after rollback`);
}

(async () => {
  const browser = await playwright[browserName].launch({ headless: true });
  try {
    for (const test of [{ width: 390, theme: "light", reduced: false }, { width: 320, theme: "dark", reduced: true }]) {
      test.name = `${test.width}-${test.theme}${test.reduced ? "-reduced" : ""}`;
      const marker = `${path.basename(output)}-${test.name}`;
      const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
      const page = await browser.newPage({ viewport: { width: test.width, height: 844 }, hasTouch: true, reducedMotion: test.reduced ? "reduce" : "no-preference" }); page.setDefaultTimeout(15000);
      const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      let releaseAssets = () => {};
      try {
        const groupId = await seed(page, files, marker);
        const summary = await api(page, "get", `gallery/detail/${groupId}?assets=0`);
        const originals = await api(page, "get", `gallery/assets/${groupId}`);
        await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#modelChoice:not(:disabled)").waitFor();
        const inner = page.frames().find((item) => item.url().includes("/ui/"));
        await inner.evaluate(async (theme) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); const Original = window.PhotoSwipe; window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__testViewer = this; } }; }, test.theme);
        await frame.locator('[data-view="gallery"]').click();
        await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
        await inner.waitForFunction((id) => { const cards = Array.from(document.querySelectorAll("[data-gallery-id]")); return cards.length === 1 && cards[0].dataset.galleryId === id; }, groupId);
        await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
        await frame.locator("#detailUseReference:not(:disabled)").waitFor();
        await installDecodeGates(inner);
        const detail = { name: "detail", selector: ".detail-image-frame img.detail-image-backdrop", sources: summary.images.map((image) => image.thumbnail_data_url), opacity: .62, navigate: (index) => clickDetailThumb(page, inner, index) };
        await exerciseSurface(page, inner, detail, test);
        await pendingDecodeDuringDrag(page, inner, detail, test);
        await page.waitForTimeout(520);
        await frame.locator("#closeDrawer").click();
        const assetsGate = new Promise((resolve) => { releaseAssets = resolve; });
        await page.route(`**/gallery/assets/${groupId}`, async (route) => { await assetsGate; try { await route.continue(); } catch (error) { if (!/already handled|closed|disposed/i.test(error.message)) throw error; } });
        await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
        await frame.locator("[data-detail-image]").waitFor();
        await frame.locator("[data-detail-image]").click(); await inner.waitForFunction(() => window.__testViewer?.opener.isOpen);
        const fullscreen = { name: "fullscreen", selector: ".pswp .image-studio-viewer-backdrop", sources: summary.images.map((image) => image.thumbnail_data_url), alternatives: originals.images.map((image) => [image.data_url]), sourceFor: (index) => inner.evaluate((index) => { const item = window.__testViewer.options.dataSource[index]; return item.previewSrc || item.originalSrc; }, index), opacity: .64, navigate: async (index) => { await inner.evaluate((index) => window.__testViewer.goTo(index), index); await inner.waitForFunction((index) => window.__testViewer.currIndex === index, index); } };
        await exerciseSurface(page, inner, fullscreen, test);
        await fullscreenMissingAndClose(page, inner, frame, fullscreen);
        assert.deepEqual(errors, [], `${test.name}: uncaught page errors`);
      } finally { releaseAssets(); await page.close(); }
    }
    console.log(`Backdrop loading screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
