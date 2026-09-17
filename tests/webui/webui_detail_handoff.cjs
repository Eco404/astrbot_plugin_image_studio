/* Model delayed primary-image resource selection and inspect the foreground at each frame. */
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-handoff-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index,(size,color) in enumerate((((640,900),(194,80,90)),((900,640),(67,148,180)),((600,850),(175,168,65)))):
 image=Image.new("RGB",size,color);draw=ImageDraw.Draw(image);draw.rectangle((15,15,90,90),fill=(245,245,245))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"handoff {marker} image {index}","model":"handoff-model","steps":24,"seed":index,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(page, files, marker) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "handoff-model" } }));
  const batch = await api(page, "post", "imports/prepare", { items, as_group: false }); assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } }); assert.ok(response.ok());
  }
  return (await api(page, "post", batch.commit_endpoint, {})).generation_ids;
}
async function touch(inner, type, dx) {
  await inner.evaluate(({ type, dx }) => {
    const frame = document.querySelector(".detail-image-frame"); const rect = frame.getBoundingClientRect();
    const point = { identifier: 41, target: frame, clientX: rect.x + rect.width * .75 + dx, clientY: rect.y + rect.height * .5 };
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [point] }, changedTouches: { value: [point] }, targetTouches: { value: type === "touchend" ? [] : [point] } }); frame.dispatchEvent(event);
  }, { type, dx });
}
async function frames(inner, count) {
  await inner.evaluate((remaining) => new Promise((resolve) => { const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick); }), count);
}
async function installProbe(inner, target) {
  await inner.evaluate((target) => {
    const frame = document.querySelector(".detail-image-frame"); const main = frame.querySelector(":scope > [data-detail-image]");
    const descriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src"); const nativeDecode = main.decode.bind(main);
    let pending = ""; let resolve;
    const gate = new Promise((done) => { resolve = done; });
    window.__handoff = { samples: [], pending: false, observing: true, old: main.src, sourceCommits: 0, mainDecodes: 0 };
    Object.defineProperty(main, "src", {
      configurable: true, get() { return descriptor.get.call(this); },
      set(value) {
        if (value === target && !window.__handoff.released) { pending = value; window.__handoff.pending = true; return; }
        descriptor.set.call(this, value);
      },
    });
    main.decode = async () => { window.__handoff.mainDecodes++; if (pending) await gate; return nativeDecode(); };
    window.__releaseHandoff = () => {
      window.__handoff.released = true;
      if (pending) { descriptor.set.call(main, pending); pending = ""; window.__handoff.sourceCommits++; }
      resolve();
    };
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1; const context = canvas.getContext("2d", { willReadFrequently: true });
    const sample = () => {
      if (!window.__handoff.observing) return;
      // During settling the outgoing pane is still legitimately crossing the
      // center. Inspect the landed foreground from handoff onward instead.
      if (window.__handoff.pending && ["handoff", "idle"].includes(frame.dataset.detailSwipeState || "idle")) {
        const rect = frame.getBoundingClientRect(); const center = rect.x + rect.width / 2;
        const overlay = frame.querySelector(".detail-swipe-overlay");
        const incoming = Array.from(frame.querySelectorAll(".detail-swipe-pane img")).find((image) => { const box = image.getBoundingClientRect(); return box.left <= center && box.right > center; });
        const visible = overlay ? incoming : main;
        let pixel = null;
        if (visible?.complete && visible.naturalWidth) { context.clearRect(0, 0, 1, 1); context.drawImage(visible, visible.naturalWidth / 2, visible.naturalHeight / 2, 1, 1, 0, 0, 1, 1); pixel = Array.from(context.getImageData(0, 0, 1, 1).data); }
        window.__handoff.samples.push({ source: visible?.src || "", mainSource: main.src, mainVisibility: getComputedStyle(main).visibility, overlay: !!overlay, visibility: visible ? getComputedStyle(visible).visibility : "missing", pixel, phase: frame.dataset.detailSwipeState || "idle" });
      }
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, target);
}
async function run(engine) {
  const browser = await engines[engine].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, deviceScaleFactor: 3 }); page.setDefaultTimeout(15000);
  const marker = `${path.basename(output)}-${engine}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const ids = [];
  try {
    ids.push(...await seed(page, files, marker));
    const sequence = (await api(page, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    const target = await api(page, "get", `gallery/detail/${sequence[1].generation_id}?assets=0`);
    await page.goto(base); const inner = page.frames().find((frame) => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.locator('[data-view="gallery"]').click(); await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${sequence[0].generation_id}"] .gallery-info`).click();
    await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    await inner.waitForFunction(() => document.querySelector(".detail-image-frame > [data-detail-image]")?.complete);
    await installProbe(inner, target.images[0].thumbnail_data_url);
    await touch(inner, "touchstart", 0); await touch(inner, "touchmove", -90); await touch(inner, "touchmove", -150); await touch(inner, "touchend", -150);
    await inner.waitForFunction(() => window.__handoff.pending);
    await inner.waitForFunction(() => document.querySelector(".detail-image-frame")?.dataset.detailSwipeState === "handoff");
    await frames(inner, 6);
    const held = await inner.evaluate(() => window.__handoff);
    assert.ok(held.samples.length >= 4);
    assert.ok(held.samples.every((sample) => sample.source !== held.old && sample.visibility === "visible" && sample.pixel?.[3] === 255), `old foreground returned while the primary source was pending: ${JSON.stringify(held.samples.map((sample) => ({ old: sample.source === held.old, overlay: sample.overlay, visibility: sample.visibility, pixel: sample.pixel, phase: sample.phase })))}`);
    assert.ok(held.mainDecodes > 0, "handoff must wait for the actual mounted main image to decode, not only an offscreen candidate");
    await page.screenshot({ path: path.join(output, `${engine}-pending-primary.png`) });
    await inner.evaluate(() => window.__releaseHandoff());
    await inner.waitForFunction(() => !document.querySelector(".detail-image-frame")?.dataset.detailSwipeState);
    await frames(inner, 6);
    const finished = await inner.evaluate(() => { window.__handoff.observing = false; return window.__handoff; });
    assert.ok(finished.samples.every((sample) => sample.source !== finished.old && sample.visibility === "visible" && sample.pixel?.[3] === 255), "old/blank image returned during overlay-to-main handoff");
    assert.ok(finished.samples.some((sample) => sample.phase === "handoff" && sample.overlay && sample.mainVisibility === "visible"), "the decoded main image must receive a render opportunity while the landed pane still covers it");
    assert.ok(finished.samples.some((sample) => !sample.overlay), "main image must eventually take over from the incoming swipe pane");
    assert.equal(await inner.locator(".detail-image-frame").getAttribute("data-generation-id"), sequence[1].generation_id);
    await page.screenshot({ path: path.join(output, `${engine}-settled-primary.png`) });
    console.log(`${engine}: pending primary decode retains landed foreground, frame-by-frame old/blank-image rejection, handoff and original upgrade passed`);
  } finally {
    await page.frames().find((frame) => frame.url().includes("/ui/"))?.evaluate(() => window.__releaseHandoff?.()).catch(() => {});
    if (ids.length) await api(page, "post", "gallery/delete", { ids });
    await page.close(); await browser.close();
  }
}
(async () => { for (const engine of ["chromium", "webkit"]) await run(engine); console.log(`Handoff screenshots: ${output}`); })().catch((error) => { console.error(error); process.exitCode = 1; });
