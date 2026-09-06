/* Real touch-drag assertions against safe fixtures in an isolated WebUI harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated Image Studio harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-detail-swipe-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(4):
 image=Image.new("RGB",(560,720),(160+index*15,205-index*8,214));draw=ImageDraw.Draw(image)
 draw.rectangle((0,470,560,720),fill=(95,142+index*10,122));draw.polygon([(0,500),(200,180),(440,500)],fill=(105+index*10,132,158));draw.ellipse((400,90,475,165),fill=(238,234,216))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"safe swipe {marker} frame {index}","model":"swipe-model","uc":"blur","steps":20+index,"seed":index,"width":560,"height":720,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}

async function seed(page, files, marker) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: files.length > 1 ? { model: "swipe-model" } : {} }));
  const batch = await api(page, "post", "imports/prepare", { items, as_group: files.length > 1 }); assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  }
  const result = await api(page, "post", batch.commit_endpoint, {}); assert.equal(result.allowed, true); return result.generation_ids[0];
}

async function frames(inner) { await inner.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); }
async function settle(inner) { await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})))); await frames(inner); }

async function selected(inner, index) {
  await inner.waitForFunction((value) => document.querySelector("[data-detail-image]")?.dataset.detailImage === String(value) && document.querySelector(`[data-detail-dot="${value}"]`)?.getAttribute("aria-current") === "true", index);
  assert.match(await inner.locator("#drawerBody").textContent(), new RegExp(`frame ${index}`));
}

async function bounds(inner) {
  return await inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame"); const dialog = document.querySelector("#detailDrawer");
    const box = (element) => { const rect = element.getBoundingClientRect(); return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }; };
    const track = frame.querySelector(".detail-swipe-track");
    const holder = frame.querySelector(":scope > .detail-image-background");
    const backdrop = holder?.querySelector(":scope > .detail-image-backdrop:not(.detail-backdrop-previous)");
    const panes = Array.from(frame.querySelectorAll(".detail-swipe-pane")).map((pane) => ({ offset: Number(pane.dataset.swipeOffset), ...box(pane), images: Array.from(pane.querySelectorAll("img")).map((image) => ({ className: image.className, ...box(image), loaded: image.complete && image.naturalWidth > 1, src: image.src, filter: getComputedStyle(image).filter })) }));
    return { frame: box(frame), dialog: box(dialog), backdrop: backdrop ? { ...box(holder), transform: getComputedStyle(holder).transform, src: backdrop.src, opacity: getComputedStyle(backdrop).opacity, filter: getComputedStyle(holder).filter } : null, track: track ? new DOMMatrixReadOnly(getComputedStyle(track).transform).m41 : null, panes, state: frame.dataset.detailSwipeState || "idle", index: Number(frame.querySelector("[data-detail-image]")?.dataset.detailImage || 0), pageWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth };
  });
}

function unchangedBox(before, after, label) {
  for (const key of ["x", "y", "width", "height"]) assert.ok(Math.abs(before[key] - after[key]) <= 1, `${label}: ${key} moved ${JSON.stringify({ before, after })}`);
}

function unchangedBackdrop(before, after, label) {
  assert.ok(before && after, `${label}: stationary backdrop must exist`);
  unchangedBox(before, after, label);
  assert.equal(after.transform, before.transform, `${label}: background transform must not follow drag`);
  assert.equal(after.src, before.src, `${label}: background must not change before committing the image`);
  assert.equal(after.opacity, before.opacity, `${label}: background remains visible throughout drag`);
  assert.match(after.filter, /blur\(/);
}

async function watchBackdropTransition(inner) {
  await inner.evaluate(() => {
    window.__swipeBackdropObserver?.disconnect();
    window.__swipeBackdropTransitions = [];
    window.__swipeBackdropObserver = new MutationObserver(() => {
      const frame = document.querySelector(".detail-image-frame");
      const previous = frame?.querySelector(".detail-image-background > .detail-backdrop-previous");
      const current = frame?.querySelector(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)");
      if (previous && current && previous.src !== current.src) window.__swipeBackdropTransitions.push({ previous: previous.src, current: current.src, index: Number(frame.querySelector("[data-detail-image]")?.dataset.detailImage) });
    });
    window.__swipeBackdropObserver.observe(document.querySelector("#drawerBody"), { subtree: true, childList: true, attributes: true, attributeFilter: ["src", "class"] });
  });
}

async function checkCommittedBackground(inner, previousSource, index, reduced) {
  const current = await bounds(inner);
  assert.notEqual(current.backdrop.src, previousSource, "background source must update only after the selected image changes");
  const transitioned = await inner.evaluate(({ previous, current, index }) => window.__swipeBackdropTransitions.some((item) => item.previous === previous && item.current === current && item.index === index), { previous: previousSource, current: current.backdrop.src, index });
  assert.equal(transitioned, true, `committed image should crossfade previous and current stationary backdrops${reduced ? " with reduced duration" : ""}`);
  await inner.locator(".detail-backdrop-previous").waitFor({ state: "detached" });
  await inner.waitForFunction(() => !document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)")?.getAnimations().some((animation) => animation.playState === "running"));
  const backgroundSource = await inner.locator(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)").getAttribute("src");
  assert.equal(backgroundSource, await inner.locator(`[data-detail-dot="${index}"] img`).getAttribute("src"), "committed background must use the selected thumbnail even when the main image has upgraded to the original");
}

async function dragStart(page, inner, client, backwards = false) {
  await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; }); await frames(inner);
  const rect = await inner.locator(".detail-image-frame").boundingBox();
  const start = { x: rect.x + rect.width * (backwards ? .24 : .76), y: rect.y + Math.min(rect.height * .55, 220) };
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ ...start, id: 0 }] });
  return start;
}

async function move(client, start, dx, dy = 0) {
  await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: start.x + dx, y: start.y + dy, id: 0 }] });
}

async function waitClean(inner) {
  await inner.locator(".detail-swipe-overlay").waitFor({ state: "detached" });
  await inner.waitForFunction(() => !document.querySelector(".detail-image-frame")?.classList.contains("is-detail-swiping"));
}

async function verifyFollow(page, inner, client, test, previews, releaseOriginals) {
  const before = await bounds(inner); const start = await dragStart(page, inner, client);
  await move(client, start, -26); await frames(inner);
  await inner.locator(".detail-swipe-track").waitFor();
  const first = await bounds(inner);
  await move(client, start, -78); await frames(inner);
  const middle = await bounds(inner);
  assert.equal(middle.state, "dragging"); assert.equal(middle.index, 0, "drag must not commit before release");
  unchangedBox(before.frame, middle.frame, "image frame"); unchangedBox(before.dialog, middle.dialog, "detail dialog");
  assert.ok(Math.abs((middle.track - first.track) + 52) <= 3, `track must follow finger delta ${JSON.stringify({ first: first.track, middle: middle.track })}`);
  const firstCurrent = first.panes.find((pane) => pane.offset === 0); const middleCurrent = middle.panes.find((pane) => pane.offset === 0);
  assert.ok(firstCurrent && middleCurrent);
  for (const className of ["detail-swipe-image"]) {
    const a = firstCurrent.images.find((image) => image.className.includes(className)); const b = middleCurrent.images.find((image) => image.className.includes(className));
    assert.ok(a && b && b.loaded, `${className} must be a loaded clone`);
    assert.ok(Math.abs((b.x - a.x) + 52) <= 3, `${className} must follow finger motion`);
  }
  unchangedBackdrop(before.backdrop, first.backdrop, "first drag backdrop");
  unchangedBackdrop(before.backdrop, middle.backdrop, "middle drag backdrop");
  assert.equal(await inner.locator(".detail-swipe-overlay .detail-image-backdrop, .detail-swipe-backdrop").count(), 0, "moving panes must not contain blurred background clones");
  const next = middle.panes.find((pane) => pane.offset === 1);
  assert.ok(next?.images.some((image) => image.loaded && image.src === previews[1]), "next preview must be available before originals load");
  assert.ok(middle.scrollWidth <= middle.pageWidth + 1, "drag must not cause horizontal page overflow");
  await page.screenshot({ path: path.join(output, `${test.name}-mid-drag.png`) });
  if (releaseOriginals) {
    releaseOriginals();
    await inner.waitForFunction((preview) => document.querySelector("[data-detail-image]")?.src !== preview, before.backdrop.src);
    const upgraded = await bounds(inner);
    assert.equal(upgraded.state, "dragging", "original arriving mid-drag must not cancel the gesture");
    unchangedBackdrop(before.backdrop, upgraded.backdrop, "original arriving during drag");
    assert.equal(upgraded.panes.find((pane) => pane.offset === 0).images.find((image) => image.className.includes("detail-swipe-image")).src, previews[0], "foreground clone must retain the preview captured at gesture start");
    await page.screenshot({ path: path.join(output, `${test.name}-original-arrived-mid-drag.png`) });
  }
  await page.waitForTimeout(140); const paused = await bounds(inner);
  assert.ok(Math.abs(paused.track - middle.track) <= .5, "paused finger must not finish a timed animation");
  unchangedBackdrop(before.backdrop, paused.backdrop, "paused drag backdrop");
  await move(client, start, -142); await frames(inner);
  await watchBackdropTransition(inner);
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await selected(inner, 1); await waitClean(inner);
  await inner.locator("[data-detail-image]").evaluate((image) => image.click());
  assert.equal(await inner.locator("#imagePreview:not(.is-hidden), .pswp--open").count(), 0, "synthetic click immediately after swipe must be suppressed");
  await checkCommittedBackground(inner, before.backdrop.src, 1, test.reduced);
  await page.screenshot({ path: path.join(output, `${test.name}-committed.png`) });
}

async function verifyRollback(page, inner, client, test) {
  await page.waitForTimeout(520);
  for (const cancelled of [false, true]) {
    const start = await dragStart(page, inner, client);
    const before = await bounds(inner);
    await move(client, start, cancelled ? -75 : -25); await frames(inner);
    assert.equal(await inner.locator(".detail-swipe-overlay").count(), 1);
    await client.send("Input.dispatchTouchEvent", { type: cancelled ? "touchCancel" : "touchEnd", touchPoints: [] });
    await waitClean(inner); await selected(inner, 1);
    unchangedBackdrop(before.backdrop, (await bounds(inner)).backdrop, "rolled back backdrop");
    assert.equal(await inner.locator(".detail-backdrop-previous").count(), 0, "rollback must not trigger an image background transition");
    await page.screenshot({ path: path.join(output, `${test.name}-${cancelled ? "cancel" : "short"}-rollback.png`) });
  }
}

async function verifyOtherGestures(page, inner, client) {
  const start = await dragStart(page, inner, client);
  await move(client, start, -8, -85); await frames(inner);
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await waitClean(inner); await selected(inner, 1);
  await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; }); await frames(inner);
  const rect = await inner.locator(".detail-image-frame").boundingBox(); const y = rect.y + rect.height / 2;
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: rect.x + 90, y, id: 0 }, { x: rect.x + 170, y, id: 1 }] });
  await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: rect.x + 55, y, id: 0 }, { x: rect.x + 205, y, id: 1 }] });
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await waitClean(inner); await selected(inner, 1);
}

async function verifyBackwards(page, inner, client, test) {
  const start = await dragStart(page, inner, client, true);
  await move(client, start, 30); await frames(inner); const first = await bounds(inner);
  await move(client, start, 80); await frames(inner); const second = await bounds(inner);
  assert.ok(Math.abs((second.track - first.track) - 50) <= 3, "reverse drag must follow rightward finger motion");
  assert.ok(second.panes.find((pane) => pane.offset === -1)?.images.some((image) => image.loaded), "previous image must be visible during reverse drag");
  unchangedBox(first.frame, second.frame, "reverse image frame"); unchangedBox(first.dialog, second.dialog, "reverse detail dialog");
  unchangedBackdrop(first.backdrop, second.backdrop, "reverse drag backdrop");
  await page.screenshot({ path: path.join(output, `${test.name}-reverse-drag.png`) });
  await move(client, start, 142); await frames(inner);
  await watchBackdropTransition(inner);
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await selected(inner, 0); await waitClean(inner);
  await checkCommittedBackground(inner, first.backdrop.src, 0, test.reduced);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const cases = [{ width: 390, theme: "light" }, { width: 390, theme: "dark" }, { width: 320, theme: "light" }, { width: 320, theme: "dark", reduced: true }];
    for (const test of cases) {
      test.name = `${test.width}-${test.theme}${test.reduced ? "-reduced" : ""}`;
      const marker = `${path.basename(output)}-${test.name}`;
      const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
      const page = await browser.newPage({ viewport: { width: test.width, height: 844 }, hasTouch: true, reducedMotion: test.reduced ? "reduce" : "no-preference" }); page.setDefaultTimeout(15000);
      const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      const adjacentId = await seed(page, files.slice(3), `${marker}-adjacent`);
      const groupId = await seed(page, files.slice(0, 3), `${marker}-group`);
      const summary = await api(page, "get", `gallery/detail/${groupId}?assets=0`);
      let release; const gate = new Promise((resolve) => { release = resolve; });
      const routePattern = `**/gallery/assets/${groupId}`;
      const delay = async (route) => {
        await gate;
        try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
      };
      await page.route(routePattern, delay);
      const requests = []; page.on("request", (request) => requests.push(request.url()));
      const client = await page.context().newCDPSession(page);
      try {
        await page.goto(base); const frame = page.frameLocator("#studio");
        await frame.locator("#modelChoice:not(:disabled)").waitFor();
        const inner = page.frames().find((item) => item.url().includes("/ui/"));
        await inner.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, test.theme);
        await frame.locator('[data-view="gallery"]').click();
        await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
        await inner.waitForFunction((ids) => { const cards = Array.from(document.querySelectorAll("[data-gallery-id]")); return cards.length === ids.length && cards.every((card) => ids.includes(card.dataset.galleryId)); }, [groupId, adjacentId]);
        await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
        await selected(inner, 0); await settle(inner);
        await verifyFollow(page, inner, client, test, summary.images.map((image) => image.thumbnail_data_url), test.theme === "dark" ? release : null);
        assert.ok(!requests.some((url) => url.includes(`/gallery/detail/${adjacentId}`) || url.includes(`/gallery/assets/${adjacentId}`)), "within-group drag must not prefetch adjacent generation");
        release(); await frame.locator("#detailUseReference:not(:disabled)").waitFor(); await selected(inner, 1);
        await verifyRollback(page, inner, client, test);
        await verifyOtherGestures(page, inner, client);
        await verifyBackwards(page, inner, client, test);
        const end = await bounds(inner); assert.ok(end.scrollWidth <= end.pageWidth + 1); assert.deepEqual(errors, []);
        console.log(`${test.name}: foreground follows real drag, background stays fixed then crossfades after commit; delayed originals, rollback, cancel, vertical/multitouch exclusion passed`);
      } finally { release(); await page.unroute(routePattern, delay); await client.detach(); await page.close(); }
    }
    console.log(`Detail swipe screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
