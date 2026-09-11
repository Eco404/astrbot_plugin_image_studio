/* Detail filmstrip checks using safe, content-unique images in an isolated harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated filmstrip harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-detail-filmstrip-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
let counter = 0;

const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(26):
 width,height=((400,540),(540,400),(480,480))[index%3]
 image=Image.new("RGB",(width,height),(160+index*2,201,211-index));draw=ImageDraw.Draw(image)
 draw.rectangle((0,height*.64,width,height),fill=(100,145+index,135));draw.polygon([(0,height*.7),(width*.4,height*.25),(width*.8,height*.7)],fill=(114,139,156));draw.ellipse((width*.7,height*.12,width*.87,height*.23),fill=(230,237,222))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"safe filmstrip {marker} image {index}","model":"filmstrip-model","uc":"blur","steps":20+index,"seed":index,"width":width,"height":height,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

async function body(response) { const value = await response.json(); return value.data || value; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`); return await body(response);
}

async function seed(page, files) {
  const items = files.map((file) => ({ client_id: `filmstrip_${Date.now().toString(36)}_${++counter}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: files.length > 1 ? { model: "filmstrip-model" } : {} }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: files.length > 1 }); assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.notEqual((await body(response)).allowed, false);
  }
  const result = await api(page, "post", prepared.commit_endpoint, {}); assert.equal(result.allowed, true); return result.generation_ids[0];
}

async function settle(inner) {
  await inner.evaluate(async () => { await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))); await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))); });
}

async function capture(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await settle(inner);
  const layout = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(layout.scroll <= layout.width + 1, `${name}: horizontal page overflow ${JSON.stringify(layout)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function selected(frame, index) {
  await frame.locator(`[data-detail-dot="${index}"][aria-current="true"]`).waitFor();
  await frame.locator(`[data-detail-image="${index}"]`).waitFor();
  assert.equal(await frame.locator('[data-detail-dot][aria-current="true"]').count(), 1);
  assert.equal(await frame.locator('[data-detail-image]').getAttribute("data-detail-image"), String(index));
}

async function backdropIdle(inner, source) {
  await inner.waitForFunction((expectedSource) => {
    const image = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    return image && image.src === expectedSource && image.dataset.backdropState === "idle"
      && !document.querySelector(".detail-backdrop-previous");
  }, source);
}

async function backdropFade(page, frame, inner, previousSource, expectedSource, name) {
  await frame.locator(".detail-backdrop-previous").waitFor({ state: "attached" });
  const before = await inner.evaluate(() => {
    const current = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    const previous = document.querySelector(".detail-backdrop-previous");
    return { current: current.src, previous: previous.src, opacity: Number(getComputedStyle(current).opacity) };
  });
  assert.ok(before.previous === previousSource, `${name}: outgoing image changed`);
  assert.ok(before.current === expectedSource, `${name}: incoming image changed`);
  assert.notEqual(before.current, before.previous, `${name}: fade requires distinct sources`);
  await inner.waitForFunction(() => {
    const current = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    const previous = document.querySelector(".detail-backdrop-previous");
    const incoming = current ? Number(getComputedStyle(current).opacity) : 0;
    const outgoing = previous ? Number(getComputedStyle(previous).opacity) : 0;
    return incoming > .02 && incoming < .58 && outgoing > .02 && outgoing < .6;
  }, null, { timeout: 1500 });
  const middle = await inner.evaluate(() => {
    const current = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    const previous = document.querySelector(".detail-backdrop-previous");
    return { current: Number(getComputedStyle(current).opacity), previous: previous ? Number(getComputedStyle(previous).opacity) : -1 };
  });
  assert.ok(middle.current > 0 && middle.current < .62 && middle.previous > 0 && middle.previous < .62, `${name}: transition snapped instead of blending ${JSON.stringify(middle)}`);
  await page.screenshot({ path: path.join(output, `${name}-mid-fade.png`) });
  await frame.locator(".detail-backdrop-previous").waitFor({ state: "detached" });
  await inner.waitForFunction(() => { const image = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)"); return image && Math.abs(Number(getComputedStyle(image).opacity) - .62) < .001; }, null, { timeout: 1500 });
  const finished = await frame.locator(".detail-image-backdrop:not(.detail-backdrop-previous)").evaluate((image) => ({ src: image.src, opacity: Number(getComputedStyle(image).opacity) }));
  assert.ok(finished.src === expectedSource, `${name}: final image changed`); assert.ok(Math.abs(finished.opacity - .62) < .001);
  await backdropIdle(inner, expectedSource);
}

async function swipe(page, inner, locator, forward = true) {
  await inner.locator(locator).evaluate((element) => element.scrollIntoView({ block: "center" })); await settle(inner);
  const rect = await inner.locator(locator).boundingBox(); const left = Math.max(18, rect.x + 35); const right = Math.min(page.viewportSize().width - 18, rect.x + rect.width - 35);
  const from = forward ? right : left; const to = forward ? left : right; const y = rect.y + rect.height / 2;
  const client = await page.context().newCDPSession(page);
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from, y }] });
  for (let step = 1; step <= 8; step++) { await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: from + (to - from) * step / 8, y }] }); await page.waitForTimeout(18); }
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }); await client.detach(); await page.waitForTimeout(100);
}

async function verifyStrip(page, frame, inner, groupId, summary, test) {
  const originalImages = (await api(page, "get", `gallery/assets/${groupId}`)).images;
  let releaseAssets; const assetsGate = new Promise((resolve) => { releaseAssets = resolve; });
  const assetPath = "**/gallery/image/*";
  const delayAssets = async (route) => { if (new URL(route.request().url()).searchParams.get("detail") === "original") await assetsGate; try { await route.continue(); } catch (error) { if (!/already handled|closed|disposed/i.test(error.message)) throw error; } };
  await page.route(assetPath, delayAssets);
  try {
    await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await frame.locator(".detail-filmstrip").waitFor();
    assert.equal(await frame.locator(".detail-filmstrip-thumb").count(), 23);
    assert.equal(await frame.locator(".detail-image-frame .detail-carousel-dots").count(), 0, "detail dots should be replaced only in the detail carousel");
    const thumbnails = summary.images.map((image) => image.thumbnail_data_url);
    const mountedThumbnails = await frame.locator(".detail-filmstrip-thumb img").evaluateAll((items) => items.map((image) => ({ index: Number(image.closest("[data-detail-dot]").dataset.detailDot), source: image.src })));
    const hiddenWidth = await inner.locator(".detail-filmstrip").evaluate((strip) => strip.scrollWidth - strip.clientWidth);
    if (hiddenWidth > 160) assert.ok(mountedThumbnails.length < thumbnails.length, "offscreen filmstrip thumbnails should remain lazy");
    assert.ok(mountedThumbnails.every((image) => image.source === thumbnails[image.index]), "loaded filmstrip images must be thumbnails for their own position");
    await settle(inner);
    await backdropIdle(inner, thumbnails[0]);
    const originalScroll = await inner.locator("#drawerBody").evaluate((element) => element.scrollTop);
    const scrollChecks = [];
    const unchangedScroll = async (step) => {
      await inner.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const value = await inner.locator("#drawerBody").evaluate((element) => element.scrollTop);
      scrollChecks.push({ step, scrollTop: value });
      assert.ok(Math.abs(value - originalScroll) <= 1, `${test.name}: ${step} scrolled main image out of view ${JSON.stringify(scrollChecks)}`);
    };
    await inner.evaluate(() => { window.__filmstrip = document.querySelector('.detail-filmstrip'); window.__filmstripButtons = Array.from(window.__filmstrip.querySelectorAll('[data-detail-dot]')); });
    await frame.locator('[data-detail-dot="3"]').click(); await selected(frame, 3);
    await unchangedScroll("click thumbnail 3");
    await backdropFade(page, frame, inner, thumbnails[0], thumbnails[3], `${test.name}-thumbnail-change`);
    await frame.locator(".detail-parameter-row").filter({ hasText: "image 3" }).first().waitFor();
    assert.equal(await frame.locator("#imagePreview:not(.is-hidden), .pswp--open").count(), 0, "thumbnail click must not open a viewer");
    await page.keyboard.press("End"); await selected(frame, 22); await unchangedScroll("End");
    await page.keyboard.press("ArrowRight"); await selected(frame, 22);
    await settle(inner);
    await inner.waitForFunction(() => { const strip = document.querySelector('.detail-filmstrip').getBoundingClientRect(); const selected = document.querySelector('[data-detail-dot][aria-current="true"]').getBoundingClientRect(); return selected.left >= strip.left - 1 && selected.right <= strip.right + 1; });
    const bounds = await inner.evaluate(() => { const strip = document.querySelector('.detail-filmstrip').getBoundingClientRect(); const current = document.querySelector('[data-detail-dot][aria-current="true"]').getBoundingClientRect(); return { strip: strip.toJSON(), current: current.toJSON(), scroll: document.querySelector('.detail-filmstrip').scrollLeft }; });
    assert.ok(bounds.current.left >= bounds.strip.left - 1 && bounds.current.right <= bounds.strip.right + 1, `selected last thumbnail must scroll into view: ${JSON.stringify(bounds)}`); assert.ok(bounds.scroll > 0);
    await page.keyboard.press("Home"); await selected(frame, 0); await unchangedScroll("Home");
    await page.keyboard.press("ArrowLeft"); await selected(frame, 0);
    await page.keyboard.press("ArrowRight"); await selected(frame, 1); await unchangedScroll("ArrowRight");
    await settle(inner);
    const previewSource = thumbnails[1];
    await backdropIdle(inner, previewSource);
    await inner.evaluate(() => {
      window.__originalUpgradeBackgrounds = [];
      window.__originalUpgradeObserver = new MutationObserver(() => {
        const background = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
        window.__originalUpgradeBackgrounds.push({ src: background?.src, previous: !!document.querySelector(".detail-backdrop-previous"), animating: background?.getAnimations().some((animation) => animation.playState === "running") });
      });
      window.__originalUpgradeObserver.observe(document.querySelector(".detail-image-frame"), { subtree: true, childList: true, attributes: true, attributeFilter: ["src", "style", "class"] });
    });
    releaseAssets(); await frame.locator("#detailUseReference:not(:disabled)").waitFor();
    const originalSource = originalImages[1].data_url;
    await inner.waitForFunction((source) => { const image = document.querySelector("[data-detail-image]"); return image?.src === source && image.complete && image.naturalWidth > 1; }, originalSource);
    await page.waitForTimeout(360);
    const upgrades = await inner.evaluate(() => { window.__originalUpgradeObserver.disconnect(); return window.__originalUpgradeBackgrounds; });
    assert.ok(upgrades.length > 0, "original upgrade must exercise the background observer");
    assert.ok(upgrades.every((value) => value.src === previewSource && !value.previous && !value.animating), "original upgrade must retain the thumbnail background without restarting a fade");
    await backdropIdle(inner, previewSource);
    assert.equal(await inner.evaluate(() => document.querySelector('.detail-filmstrip') === window.__filmstrip && Array.from(document.querySelectorAll('[data-detail-dot]')).every((button, index) => button === window.__filmstripButtons[index])), true, "same-group selections and original upgrades must keep the strip DOM");
    await selected(frame, 1);
    await unchangedScroll("original loaded");
    const upgradedThumbnails = await frame.locator(".detail-filmstrip-thumb img").evaluateAll((items) => items.map((image) => ({ index: Number(image.closest("[data-detail-dot]").dataset.detailDot), source: image.src })));
    assert.ok(upgradedThumbnails.every((image) => image.source === thumbnails[image.index]), "original upgrade must not replace filmstrip thumbnails with originals");
    await inner.waitForFunction(() => { const strip = document.querySelector('.detail-filmstrip').getBoundingClientRect(); const active = document.querySelector('[data-detail-dot][aria-current="true"]').getBoundingClientRect(); return active.left >= strip.left - 1 && active.right <= strip.right + 1; });
    await capture(page, inner, `${test.name}-filmstrip-upgraded`);
    await inner.evaluate(() => { for (const index of [2, 3, 4, 2, 1]) document.querySelector(`[data-detail-dot="${index}"]`).click(); });
    await selected(frame, 1); await settle(inner); await backdropIdle(inner, previewSource);
    assert.ok(await frame.locator(".detail-image-backdrop:not(.detail-backdrop-previous)").getAttribute("src") === previewSource, "rapid selections left a stale backdrop");
    await unchangedScroll("rapid selection fade");
    const reducedSource = thumbnails[2];
    await frame.locator('[data-detail-dot="2"]').click(); await selected(frame, 2); await backdropIdle(inner, reducedSource);
    await frame.locator('[data-detail-dot="1"]').click(); await selected(frame, 1); await backdropIdle(inner, previewSource);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await frame.locator('[data-detail-dot="2"]').click(); await selected(frame, 2);
    await backdropFade(page, frame, inner, previewSource, reducedSource, `${test.name}-reduced-short-fade`);
    await frame.locator('[data-detail-dot="1"]').click(); await selected(frame, 1); await unchangedScroll("reduced-motion selection");
    await backdropIdle(inner, previewSource);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    if (test.width <= 540) {
      await frame.locator('[data-detail-dot="1"]').press("Home"); await selected(frame, 0);
      const before = await inner.locator('.detail-filmstrip').evaluate((element) => element.scrollLeft);
      await swipe(page, inner, ".detail-filmstrip"); await selected(frame, 0);
      assert.ok(await inner.locator('.detail-filmstrip').evaluate((element) => element.scrollLeft) > before + 10, "touch swipe should scroll the filmstrip");
      assert.equal(await frame.locator(".detail-filmstrip-thumb").count(), 23);
      await capture(page, inner, `${test.name}-filmstrip-scrolled`);
    }
    console.log(`${test.name}: main-image vertical scroll ${JSON.stringify(scrollChecks)}`);
  } finally { releaseAssets(); await page.unroute(assetPath, delayAssets); }
}

async function verifyViewerReturn(page, frame, inner, test) {
  const active = frame.locator('[data-detail-dot][aria-current="true"]'); await active.press("Home"); await selected(frame, 0);
  await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; });
  if (test.width <= 540) {
    await frame.locator('[data-detail-image]').click(); await inner.waitForFunction(() => window.__testViewer?.opener.isOpen);
    await inner.evaluate(() => window.__testViewer.goTo(window.__testViewer.currIndex + 4));
    await inner.waitForFunction(() => window.__testViewer.currSlide.data.image_index === 4);
    await selected(frame, 0);
    await inner.evaluate(() => window.__testViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" }); await selected(frame, 4);
  } else {
    await frame.locator('[data-detail-image]').click(); await frame.locator("#imagePreview:not(.is-hidden)").waitFor();
    assert.equal(await frame.locator("#imagePreviewDots .detail-carousel-dot").count(), 23, "separate preview should retain its dots");
    await frame.locator("#imagePreviewNext").click(); await frame.locator("#closeImagePreview").click(); await selected(frame, 1);
  }
  await capture(page, inner, `${test.name}-viewer-return-selection`);
}

async function verifyCrossGroup(page, frame, inner, singleId, shortId, test) {
  await frame.locator('[data-detail-dot][aria-current="true"]').press("End"); await selected(frame, 22);
  if (test.width <= 540) await swipe(page, inner, ".detail-image-frame"); else await frame.locator('[data-detail-nav="1"]').click();
  await inner.waitForFunction((id) => { const frame = document.querySelector(".detail-image-frame"); return frame?.dataset.generationId === id && !frame.dataset.detailSwipeState && frame.getAttribute("aria-busy") !== "true"; }, singleId); await frame.locator('[data-detail-image]').waitFor();
  assert.equal(await frame.locator(".detail-filmstrip").count(), 0, "single-image record must hide the filmstrip");
  if (test.width <= 540) await swipe(page, inner, ".detail-image-frame"); else await frame.locator('[data-detail-nav="1"]').click();
  await inner.waitForFunction((id) => { const frame = document.querySelector(".detail-image-frame"); return frame?.dataset.generationId === id && !frame.dataset.detailSwipeState && frame.getAttribute("aria-busy") !== "true"; }, shortId); await frame.locator(".detail-filmstrip").waitFor(); assert.equal(await frame.locator(".detail-filmstrip-thumb").count(), 2);
  await selected(frame, 0); await capture(page, inner, `${test.name}-adjacent-group-filmstrip`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [1440, 390, 320]) for (const theme of ["light", "dark"]) {
      const test = { width, theme, name: `${width}-${theme}` };
      const marker = `${path.basename(output)}-${test.name}`;
      const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
      const page = await browser.newPage({ viewport: { width, height: width <= 540 ? 844 : 1000 }, hasTouch: width <= 540 }); page.setDefaultTimeout(12000);
      const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const shortId = await seed(page, files.slice(24)); const singleId = await seed(page, files.slice(23, 24)); const groupId = await seed(page, files.slice(0, 23));
      const summary = await api(page, "get", `gallery/detail/${groupId}?assets=0`);
      const inner = page.frames().find((item) => item.url().includes("/ui/"));
      await inner.evaluate(async (theme) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); const Original = window.PhotoSwipe; window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__testViewer = this; } }; }, theme);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
      await inner.waitForFunction((ids) => { const cards = Array.from(document.querySelectorAll("[data-gallery-id]")); return cards.length === ids.length && cards.every((card) => ids.includes(card.dataset.galleryId)); }, [groupId, singleId, shortId]);
      await verifyStrip(page, frame, inner, groupId, summary, test);
      await verifyViewerReturn(page, frame, inner, test);
      await verifyCrossGroup(page, frame, inner, singleId, shortId, test);
      assert.deepEqual(errors, [], `${test.name}: browser errors`); await page.close(); console.log(`${test.name}: group-only thumbnails, preserved DOM, keyboard, swipe, original upgrade, viewer synchronization passed`);
    }
    console.log(`Filmstrip screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
