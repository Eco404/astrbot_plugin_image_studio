/* Foreground paint stability with controlled decode latency and synthetic images. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated image-paint harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-image-paint-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
let sequence = 0;

const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index, palette in enumerate(((190,211,160),(212,170,183),(151,185,217))):
 width,height=((600,900),(900,600),(700,700))[index]
 image=Image.new("RGB",(width,height),palette);draw=ImageDraw.Draw(image)
 draw.rectangle((0,height*.66,width,height),fill=(70+index*20,130,121));draw.polygon([(0,height*.68),(width*.4,height*.24),(width*.85,height*.68)],fill=(95,120+index*30,144));draw.ellipse((width*.7,height*.12,width*.87,height*.23),fill=(243,237,207))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"{marker} landscape image {index}","model":"paint-fixture","steps":21+index,"seed":index,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

async function body(response) { const result = await response.json(); return result.data || result; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {}); assert.ok(response.ok(), `${endpoint}: ${response.status()}`); return await body(response);
}

async function seed(page, files) {
  const items = files.map((file) => ({ client_id: `paint_${Date.now().toString(36)}_${++sequence}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "paint-fixture" } }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: true }); assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) { const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } }); assert.ok(response.ok()); }
  return (await api(page, "post", prepared.commit_endpoint, {})).generation_id;
}

async function frames(inner, count = 2) {
  await inner.evaluate((amount) => new Promise((resolve) => { const next = () => amount-- <= 0 ? resolve() : requestAnimationFrame(next); next(); }), count);
}

async function paint(inner) {
  return await inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame"); const main = frame?.querySelector(":scope > .detail-image[data-detail-image]");
    return { src: main?.getAttribute("src"), index: main?.dataset.detailImage, opacity: main ? Number(getComputedStyle(main).opacity) : 0, width: main?.naturalWidth || 0, complete: main?.complete, sameFrame: frame === window.__paintFrame, sameMain: main === window.__paintMain, sameHolder: frame?.querySelector(":scope > .detail-image-background") === window.__paintHolder, sameBackdrop: frame?.querySelector(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)") === window.__paintBackdrop, scroll: document.getElementById("drawerBody").scrollTop };
  });
}

async function gate(inner, source) { await inner.evaluate((src) => window.__paintBlocked.add(src), source); }
async function release(inner, source) { await inner.evaluate((src) => { window.__paintBlocked.delete(src); for (const item of window.__paintPending.filter((entry) => entry.src === src)) item.resolve(); window.__paintPending = window.__paintPending.filter((entry) => entry.src !== src); }, source); }
async function mainSource(inner, source) { await inner.waitForFunction((src) => document.querySelector(".detail-image-frame > .detail-image[data-detail-image]")?.src === src, source); await frames(inner); }
async function clickThumbnail(page, frame, index) { const rect = await frame.locator(`[data-detail-dot="${index}"]`).boundingBox(); await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2); }

async function installPaintProbe(inner) {
  await inner.evaluate(() => {
    const original = HTMLImageElement.prototype.decode;
    window.__paintBlocked = new Set(); window.__paintPending = []; window.__paintDecodeFailures = new Map();
    HTMLImageElement.prototype.decode = async function () {
      const source = this.src;
      const foreground = !String(this.className).includes("backdrop");
      if (foreground && window.__paintBlocked.has(source)) await new Promise((resolve) => window.__paintPending.push({ src: source, className: this.className, resolve }));
      if (foreground && (window.__paintDecodeFailures.get(source) || 0) > 0) { window.__paintDecodeFailures.set(source, window.__paintDecodeFailures.get(source) - 1); throw new Error("测试图片解码暂时失败"); }
      return await original.call(this);
    };
    const OriginalViewer = window.PhotoSwipe;
    window.PhotoSwipe = class extends OriginalViewer { constructor(options) { super(options); window.__paintViewer = this; } };
    window.__paintSamples = []; window.__paintSampling = false;
    window.__samplePaint = () => {
      if (!window.__paintSampling) return;
      const frame = document.querySelector(".detail-image-frame"); const main = frame?.querySelector(":scope > .detail-image[data-detail-image]");
      const overlay = frame?.querySelector(".detail-swipe-overlay");
      const viewer = window.__paintViewer;
      const visible = viewer?.opener?.isOpen ? viewer.currSlide.content.element : overlay ? overlay.querySelector('.detail-swipe-pane[data-swipe-offset="0"] img') : main;
      if (visible) window.__paintSamples.push({ scope: viewer?.opener?.isOpen ? "viewer" : overlay ? "gesture" : "detail", tag: visible.tagName, hasSource: !!visible.getAttribute("src"), opacity: Number(getComputedStyle(visible).opacity), width: visible.naturalWidth || 0, height: visible.naturalHeight || 0 });
      requestAnimationFrame(window.__samplePaint);
    };
  });
}

async function capture(page, inner, name) {
  await frames(inner); const rect = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(rect.scroll <= rect.width + 1, `${name}: horizontal overflow`); await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function swipe(page, inner) {
  const rect = await inner.locator(".detail-image-frame").boundingBox(); const right = Math.min(page.viewportSize().width - 35, rect.x + rect.width - 35); const left = Math.max(35, rect.x + 35); const y = rect.y + rect.height / 2;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: right, y }] });
  for (let step = 1; step <= 8; step++) { await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: right + (left - right) * step / 8, y }] }); await page.waitForTimeout(18); }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }); await cdp.detach();
}

async function verifyDetail(page, frame, inner, groupId, summary, originals, test) {
  const previews = summary.images.map((item) => item.thumbnail_data_url);
  assert.ok(previews.every((src, index) => src !== originals[index]), "preview and original fixtures must be different resources");
  let releaseAssets; const pendingAssets = new Promise((resolve) => { releaseAssets = resolve; });
  const delay = async (route) => { await pendingAssets; try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; } };
  await page.route(`**/gallery/assets/${groupId}`, delay);
  const scrollHistory = [];
  const recordScroll = async (step) => { scrollHistory.push({ step, scroll: (await paint(inner)).scroll }); };
  try {
    // Neighbor previews are prepared on opening, so hold decoding before prefetch starts.
    await gate(inner, previews[1]);
    await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click(); await frame.locator(".detail-filmstrip").waitFor(); await mainSource(inner, previews[0]);
    await inner.evaluate(() => { window.__paintFrame = document.querySelector('.detail-image-frame'); window.__paintMain = window.__paintFrame.querySelector(':scope > .detail-image[data-detail-image]'); window.__paintHolder = window.__paintFrame.querySelector(':scope > .detail-image-background'); window.__paintBackdrop = window.__paintHolder.querySelector(':scope > .detail-image-backdrop:not(.detail-backdrop-previous)'); window.__paintSampling = true; requestAnimationFrame(window.__samplePaint); });
    await recordScroll("initial");
    await clickThumbnail(page, frame, 1);
    await inner.waitForFunction((src) => window.__paintPending.some((entry) => entry.src === src), previews[1]); await frames(inner, 4);
    const held = await paint(inner); assert.equal(held.src, previews[0], "old foreground must remain while new preview decodes"); assert.equal(held.opacity, 1); assert.ok(held.sameMain && held.sameFrame && held.sameHolder && held.sameBackdrop);
    await recordScroll("preview held");
    await capture(page, inner, `${test.name}-preview-decode-held`);
    await release(inner, previews[1]); await mainSource(inner, previews[1]);
    await recordScroll("preview ready");
    let targetIndex = 1;
    if (test.width <= 540) {
      await swipe(page, inner); targetIndex = 2;
      await frame.locator('[data-detail-dot="2"][aria-current="true"]').waitFor(); await mainSource(inner, previews[2]);
      await recordScroll("swipe landed");
      const landed = await paint(inner); assert.ok(landed.sameFrame && landed.sameMain && landed.sameHolder, "gesture commit rebuilt image layers");
    }
    await gate(inner, originals[targetIndex]); releaseAssets(); await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    await inner.waitForFunction((src) => window.__paintPending.some((entry) => entry.src === src), originals[targetIndex]);
    const waitingOriginal = await paint(inner); assert.equal(waitingOriginal.src, previews[targetIndex], "preview must remain until original decode completes"); assert.ok(waitingOriginal.width > 1 && waitingOriginal.sameMain && waitingOriginal.sameHolder && waitingOriginal.sameBackdrop);
    await recordScroll("original held");
    await capture(page, inner, `${test.name}-original-decode-held`);
    await release(inner, originals[targetIndex]); await mainSource(inner, originals[targetIndex]);
    await recordScroll("original ready");
    const staleIndex = targetIndex === 1 ? 2 : 1;
    await gate(inner, originals[staleIndex]); await clickThumbnail(page, frame, staleIndex);
    await inner.waitForFunction((src) => window.__paintPending.some((entry) => entry.src === src), originals[staleIndex]);
    await clickThumbnail(page, frame, 0); await mainSource(inner, originals[0]);
    await release(inner, originals[staleIndex]); await frames(inner, 5); assert.equal((await paint(inner)).src, originals[0], "late previous selection replaced the current image");
    await recordScroll("stale released");
    const final = await paint(inner); assert.equal(final.opacity, 1); assert.equal(final.scroll, scrollHistory[0].scroll, JSON.stringify(scrollHistory)); assert.ok(final.sameFrame && final.sameMain && final.sameHolder && final.sameBackdrop);
    await capture(page, inner, `${test.name}-detail-decoded`);
  } finally { releaseAssets(); await page.unroute(`**/gallery/assets/${groupId}`, delay); }
}

async function verifyViewer(page, frame, inner, originals, test) {
  if (test.width > 540) return;
  const groupId = await frame.locator(".detail-filmstrip").getAttribute("data-generation-id");
  await frame.locator("#closeDrawer").click();
  let releaseAssets; const pendingAssets = new Promise((resolve) => { releaseAssets = resolve; });
  const delayed = async (route) => { await pendingAssets; try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; } };
  await page.route(`**/gallery/assets/${groupId}`, delayed);
  try {
  await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
  await inner.waitForFunction(() => document.querySelector(".detail-image-frame > .detail-image[data-detail-image]")?.naturalWidth > 1);
  await inner.evaluate((src) => window.__paintDecodeFailures.set(src, 1), originals[2]);
  await frame.locator('[data-detail-image]').click(); await inner.waitForFunction(() => window.__paintViewer?.opener?.isOpen);
  const source = await inner.evaluate(() => window.__paintViewer.currSlide.content.loadImage.toString());
  assert.ok(/\.src\s*=/.test(source), "PhotoSwipe loadImage should update its existing image source");
  await inner.waitForFunction(() => window.__paintViewer.currSlide.content.element?.naturalWidth > 1);
  const preview = await inner.evaluate(() => window.__paintViewer.options.dataSource[2].previewSrc);
  await inner.evaluate(() => window.__paintViewer.goTo(2));
  await frame.locator(".image-studio-image-status:not([hidden])").waitFor();
  assert.equal(await inner.evaluate(() => window.__paintViewer.currSlide.content.element?.tagName), "IMG");
  assert.equal(await inner.evaluate(() => window.__paintViewer.currSlide.content.element?.src), preview);
  await frame.locator(".image-studio-image-status button").click();
  await inner.waitForFunction((src) => window.__paintViewer.currSlide.data.originalSrc === src, originals[2]);
  for (const index of [1, 0, 2, 0, 1, 2]) { await inner.evaluate((value) => window.__paintViewer.goTo(value), index); await frames(inner, 3); }
  await inner.waitForFunction(() => window.__paintViewer.currSlide.content.state === "loaded" && window.__paintViewer.currSlide.content.element?.naturalWidth > 1);
  await capture(page, inner, `${test.name}-viewer-repeated`);
  await inner.evaluate(() => window.__paintViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
  } finally { releaseAssets(); await page.unroute(`**/gallery/assets/${groupId}`, delayed); }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [{ width: 1440, theme: "light" }, { width: 390, theme: "light" }, { width: 320, theme: "dark" }]) {
      test.name = `${test.width}-${test.theme}`; const marker = `${path.basename(output)}-${test.name}`;
      const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
      const page = await browser.newPage({ viewport: { width: test.width, height: test.width <= 540 ? 844 : 1000 }, hasTouch: test.width <= 540 }); page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const groupId = await seed(page, files); const summary = await api(page, "get", `gallery/detail/${groupId}?assets=0`); const assets = await api(page, "get", `gallery/assets/${groupId}`); const originals = assets.images.map((image) => image.data_url);
      const inner = page.frames().find((item) => item.url().includes("/ui/")); await inner.evaluate(async (theme) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme); await installPaintProbe(inner);
      await frame.locator('[data-view="gallery"]').click(); await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab"); await frame.locator(`[data-gallery-id="${groupId}"]`).waitFor();
      await verifyDetail(page, frame, inner, groupId, summary, originals, test); await verifyViewer(page, frame, inner, originals, test);
      const invalid = await inner.evaluate(() => { window.__paintSampling = false; return window.__paintSamples.filter((sample) => sample.scope !== "gesture" && (sample.tag !== "IMG" || !sample.hasSource || sample.width < 2 || sample.height < 2 || sample.opacity < .999)); });
      assert.deepEqual(invalid, [], `${test.name}: empty or translucent foreground paint samples`); assert.deepEqual(errors, []);
      await page.close(); console.log(`${test.name}: decoded foreground swaps, persistent DOM, stale-load guards, gesture landing and viewer paint passed`);
    }
    console.log(`Image paint screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
