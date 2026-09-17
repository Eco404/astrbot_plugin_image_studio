/* Observe actual painted pixels while the real detail UI hands cached images
 * back from swipe panes; checking the final cursor alone misses a one-frame flash.
 */
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-cached-handoff-"));
const marker = path.basename(output);
const colors = [[202, 48, 37], [40, 72, 205], [43, 173, 65], [195, 61, 178], [213, 177, 44], [38, 174, 190]];
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];colors=json.loads(sys.argv[3]);paths=[]
for index,color in enumerate(colors):
 image=Image.new("RGB",(720,900),tuple(color));draw=ImageDraw.Draw(image)
 draw.rectangle((20,20,120,100),fill=(245,245,245))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI")
 metadata.add_text("Comment",json.dumps({"prompt":f"{marker} cached handoff {index}","model":"handoff-fixture","steps":24,"seed":index,"request_type":"PromptGenerateRequest"}))
 file=folder/f"{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;
function gate() { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; }
async function api(client, method, endpoint, data) {
  const response = await client[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(client, files, start) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${start + index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "handoff-fixture" } }));
  const prepared = await api(client, "post", "imports/prepare", { items, as_group: true });
  assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await client.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok());
  }
  const result = await api(client, "post", prepared.commit_endpoint, {});
  const id = result.generation_ids[0];
  const detail = await api(client, "get", `gallery/detail/${id}?assets=0`);
  return { id, images: detail.images.map((image, index) => ({ id: image.id, color: colors[start + index] })) };
}
async function frames(frame, count = 3) {
  await frame.evaluate(remaining => new Promise(resolve => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick);
  }), count);
}
async function selected(frame, item, { display = false, loaded = true } = {}) {
  await frame.waitForFunction(({ item, display, loaded }) => {
    const holder = document.querySelector(".detail-image-frame"), image = holder?.querySelector("[data-detail-image]");
    return holder?.dataset.generationId === item.generation_id && image?.dataset.imageKey === item.image_id
      && !holder.dataset.detailSwipeState && (!loaded || image?.complete && image.naturalWidth > 1)
      && (!display || image?.naturalWidth === item.width && image.naturalHeight === item.height);
  }, { item, display, loaded });
}
async function touch(frame, type, dx = 0) {
  return await frame.evaluate(({ type, dx }) => window.__cachedHandoff.touch(type, dx), { type, dx });
}
async function swipe(frame, direction = 1) {
  await touch(frame, "touchstart"); await touch(frame, "touchmove", -direction * 90);
  assert.equal(await touch(frame, "touchmove", -direction * 165), "dragging", "the next swipe must be accepted before waiting for image work");
  await touch(frame, "touchend", -direction * 165);
}
async function observe(frame, palette) {
  await frame.evaluate(palette => {
    const holder = document.querySelector(".detail-image-frame"), main = holder.querySelector("[data-detail-image]");
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const state = { phase: "warming", allowBlank: true, violations: [], samples: [], running: true, ends: 0, paints: 0 };
    const descriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
    const decode = main.decode.bind(main);
    let mountedGate = null;
    Object.defineProperty(main, "src", {
      configurable: true,
      get() { return descriptor.get.call(this); },
      set(source) {
        if (mountedGate && !mountedGate.released && this.dataset.imageKey === mountedGate.target) {
          mountedGate.source = source; mountedGate.pending = true; return;
        }
        descriptor.set.call(this, source);
      },
    });
    main.decode = async () => {
      if (mountedGate?.pending && !mountedGate.released && main.dataset.imageKey === mountedGate.target) await mountedGate.promise;
      return decode();
    };
    let stamp = performance.now();
    const pixelIdentity = image => {
      if (!image?.getAttribute("src") || !image.complete || image.naturalWidth < 2) return null;
      context.clearRect(0, 0, 1, 1);
      context.drawImage(image, Math.floor(image.naturalWidth / 2), Math.floor(image.naturalHeight / 2), 1, 1, 0, 0, 1, 1);
      const pixel = Array.from(context.getImageData(0, 0, 1, 1).data);
      return palette.find(item => item.color.every((channel, index) => Math.abs(pixel[index] - channel) <= 6))?.id || `unexpected:${pixel}`;
    };
    const sample = event => {
      if (!state.running || state.phase === "warming") return;
      const overlay = holder.querySelector(".detail-swipe-overlay");
      const id = main.dataset.imageKey;
      const visible = getComputedStyle(main).visibility !== "hidden";
      const pixel = pixelIdentity(main);
      const record = { event, phase: state.phase, gesture: holder.dataset.detailSwipeState || "idle", id, pixel, overlay: !!overlay, visible };
      state.samples.push(record);
      if (main !== holder.querySelector(":scope > [data-detail-image]") || holder !== document.querySelector(".detail-image-frame")) state.violations.push({ ...record, error: "persistent foreground was replaced" });
      if (!overlay && visible && (pixel && pixel !== id || !pixel && !state.allowBlank)) state.violations.push(record);
      if (event === "end") state.ends++;
    };
    holder.addEventListener("detail-swipe-end", () => sample("end"));
    const observer = new MutationObserver(records => { state.paints += records.length; });
    observer.observe(main, { attributes: true, attributeFilter: ["src"] });
    const tick = () => { if (!state.running) return; sample("raf"); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    state.touch = (type, dx) => {
      const rect = holder.getBoundingClientRect();
      const point = { identifier: 79, target: holder, clientX: rect.x + rect.width * .6 + dx, clientY: rect.y + Math.min(140, rect.height * .4) };
      const active = type === "touchend" || type === "touchcancel" ? [] : [point];
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, { touches: { value: active }, targetTouches: { value: active }, changedTouches: { value: [point] }, timeStamp: { value: stamp += 25 } });
      holder.dispatchEvent(event); return holder.dataset.detailSwipeState || "idle";
    };
    state.begin = (phase, allowBlank = false) => { state.phase = phase; state.allowBlank = allowBlank; };
    state.holdMain = target => {
      let release;
      const promise = new Promise(resolve => { release = resolve; });
      mountedGate = { target, promise, release, pending: false, released: false, source: "" };
    };
    state.releaseMain = () => {
      if (!mountedGate) return;
      mountedGate.released = true;
      if (mountedGate.source && main.dataset.imageKey === mountedGate.target) descriptor.set.call(main, mountedGate.source);
      mountedGate.release();
    };
    state.handoff = () => {
      const center = holder.getBoundingClientRect().left + holder.clientWidth / 2;
      const pane = Array.from(holder.querySelectorAll(".detail-swipe-pane img")).find(image => {
        const rect = image.getBoundingClientRect(); return rect.left <= center && rect.right > center;
      });
      return { pending: !!mountedGate?.pending, phase: holder.dataset.detailSwipeState || "idle", overlay: !!holder.querySelector(".detail-swipe-overlay"), pixel: pixelIdentity(pane || main) };
    };
    state.stop = () => { state.running = false; observer.disconnect(); return { samples: state.samples, violations: state.violations, ends: state.ends, paints: state.paints }; };
    window.__cachedHandoff = state;
  }, palette);
}
async function phase(frame, name, allowBlank = false) {
  await frame.evaluate(({ name, allowBlank }) => window.__cachedHandoff.begin(name, allowBlank), { name, allowBlank });
}
async function run(browser, engine, viewport, sequence, palette) {
  const page = await browser.newPage({ viewport, hasTouch: true, deviceScaleFactor: viewport.width < 540 ? 3 : 2 });
  page.setDefaultTimeout(15000);
  const errors = [], previewGate = gate(), displayGate = gate();
  const cold = sequence[5], upgrade = sequence[4];
  let displayHeld = false;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/gallery/image/*", async route => {
    const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1), quality = url.searchParams.get("detail");
    if (id === cold.image_id && ["preview", "display"].includes(quality)) await previewGate.promise;
    if (id === upgrade.image_id && quality === "display") { displayHeld = true; await displayGate.promise; }
    try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
  });
  try {
    await page.goto(base); const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
    await frame.locator(`[data-gallery-id="${sequence[0].generation_id}"] .gallery-info`).click();
    await selected(frame, sequence[0], { display: true });
    await observe(frame, palette);
    // Warm the actual shared preview/display and decode caches by browsing.
    for (const item of sequence.slice(1, 4)) { await frame.locator('[data-detail-nav="1"]').evaluate(button => button.click()); await selected(frame, item, { display: true }); }
    for (const item of sequence.slice(0, 3).reverse()) { await frame.locator('[data-detail-nav="-1"]').evaluate(button => button.click()); await selected(frame, item, { display: true }); }
    await phase(frame, "warm same-group and cross-group");
    for (const item of sequence.slice(1, 4)) { await swipe(frame); await selected(frame, item, { display: true }); await frames(frame, 4); }
    for (const item of sequence.slice(0, 3).reverse()) { await swipe(frame, -1); await selected(frame, item, { display: true }); await frames(frame, 4); }
    assert.deepEqual(await frame.evaluate(() => window.__cachedHandoff.violations.slice(0, 10)), [], "cached image navigation exposed old/blank foreground pixels at overlay removal");

    await phase(frame, "mounted image selection waits beneath landed pane");
    await frame.evaluate(id => window.__cachedHandoff.holdMain(id), sequence[1].image_id);
    await swipe(frame);
    await frame.waitForFunction(() => window.__cachedHandoff.handoff().pending && window.__cachedHandoff.handoff().phase === "handoff");
    await frames(frame, 8);
    assert.deepEqual(await frame.evaluate(() => window.__cachedHandoff.handoff()), { pending: true, phase: "handoff", overlay: true, pixel: sequence[1].image_id }, "the landed pane must cover the old mounted pixels until the mounted target is ready");
    for (const interaction of ["tap", "cancel short drag", "cancel pending handoff"]) {
      await phase(frame, `held mounted image: ${interaction}`);
      if (interaction === "tap") {
        await touch(frame, "touchstart"); await touch(frame, "touchend");
      } else if (interaction === "cancel short drag") {
        await touch(frame, "touchstart");
        assert.equal(await touch(frame, "touchmove", -38), "dragging");
        await touch(frame, "touchcancel", -38);
      } else await touch(frame, "touchcancel");
      await frame.waitForFunction(() => window.__cachedHandoff.handoff().phase === "handoff");
      await frames(frame, 6);
      assert.deepEqual(await frame.evaluate(() => window.__cachedHandoff.handoff()), { pending: true, phase: "handoff", overlay: true, pixel: sequence[1].image_id }, `${interaction}: a cancelled second gesture must keep the landed image over the old mounted pixels`);
      assert.deepEqual(await frame.evaluate(() => window.__cachedHandoff.violations.slice(0, 10)), [], `${interaction}: cancellation must not expose an unready main image`);
    }
    await frame.evaluate(() => window.__cachedHandoff.releaseMain());
    await selected(frame, sequence[1], { display: true }); await frames(frame, 4);

    await phase(frame, "next touch takes over a held mounted image");
    await frame.evaluate(id => window.__cachedHandoff.holdMain(id), sequence[2].image_id);
    await swipe(frame);
    await frame.waitForFunction(() => window.__cachedHandoff.handoff().pending && window.__cachedHandoff.handoff().phase === "handoff");
    await frames(frame, 4);
    await swipe(frame); await selected(frame, sequence[3], { display: true });
    await frame.evaluate(() => window.__cachedHandoff.releaseMain()); await frames(frame, 5);
    await selected(frame, sequence[3], { display: true });
    for (const item of sequence.slice(0, 3).reverse()) { await swipe(frame, -1); await selected(frame, item, { display: true }); }
    await phase(frame, "continuous cached handover");
    await swipe(frame); await frames(frame, 2);
    const beforeSecond = await frame.locator(".detail-image-frame").getAttribute("data-detail-swipe-state");
    assert.ok(beforeSecond, "second gesture must start before the first settles");
    await swipe(frame); await frames(frame, 2); await swipe(frame);
    await selected(frame, sequence[3], { display: true }); await frames(frame, 6);

    await phase(frame, "same-image quality upgrade", true);
    await swipe(frame); await selected(frame, upgrade);
    for (let attempt = 0; attempt < 80 && !displayHeld; attempt++) await page.waitForTimeout(25);
    assert.ok(displayHeld, "the screen-sized image request must be in flight before the next touch");
    const preview = await frame.locator(".detail-image-frame > [data-detail-image]").getAttribute("src");
    await touch(frame, "touchstart"); await touch(frame, "touchmove", -45);
    displayGate.release();
    await frames(frame, 12);
    assert.equal(await frame.locator(".detail-image-frame > [data-detail-image]").getAttribute("src"), preview, "same-image resolution upgrades must still wait until the gesture finishes");
    await touch(frame, "touchcancel", -45);
    await selected(frame, upgrade, { display: true });

    await phase(frame, "cold response after navigating away", true);
    await swipe(frame); await selected(frame, cold, { loaded: false });
    assert.equal(await frame.locator(".detail-image-frame > [data-detail-image]").getAttribute("src"), null, "a missing target must clear the previous foreground, not expose it under the new cursor");
    await swipe(frame, -1); await selected(frame, upgrade, { display: true });
    previewGate.release(); await frames(frame, 16);
    await selected(frame, upgrade, { display: true });
    const result = await frame.evaluate(() => window.__cachedHandoff.stop());
    fs.writeFileSync(path.join(output, `${engine}-${viewport.width}-samples.json`), JSON.stringify(result, null, 2));
    assert.deepEqual(result.violations, [], `previous/blank foreground was exposed during handoff: ${JSON.stringify(result.violations.slice(0, 10))}`);
    assert.ok(result.ends >= 8, "observe actual overlay removal events, not only final stable frames");
    assert.ok(result.samples.some(sample => sample.gesture === "handoff"), "include the underlay render frames before overlay removal");
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, `${engine}-${viewport.width}.png`) });
    console.log(`${engine}-${viewport.width}: cached same/cross-group pixels at every frame, mounted decode handoff and interruption, deferred same-image upgrades and late cold previews passed`);
  } finally {
    previewGate.release(); displayGate.release();
    await page.frames().find(item => item.url().includes("/ui/"))?.evaluate(() => window.__cachedHandoff?.releaseMain()).catch(() => {});
    await page.close();
  }
}

(async () => {
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker, JSON.stringify(colors)], { encoding: "utf8" }));
  const client = await engines.request.newContext(), groups = [];
  try {
    // Insert the cold group first so it follows the four warm images in gallery order.
    for (const start of [4, 2, 0]) groups.push(await seed(client, files.slice(start, start + 2), start));
    const sequence = (await api(client, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    const palette = groups.flatMap(group => group.images);
    assert.equal(sequence.length, 6);
    assert.deepEqual(sequence.map(item => item.image_id), [...groups].reverse().flatMap(group => group.images.map(image => image.id)));
    for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
      assert.ok(["chromium", "webkit"].includes(engine));
      const browser = await engines[engine].launch({ headless: true });
      try { for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }]) await run(browser, engine, viewport, sequence, palette); }
      finally { await browser.close(); }
    }
    console.log(`Cached detail handoff artifacts: ${output}`);
  } finally { if (groups.length) await api(client, "post", "gallery/delete", { ids: groups.map(group => group.id) }); await client.dispose(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
