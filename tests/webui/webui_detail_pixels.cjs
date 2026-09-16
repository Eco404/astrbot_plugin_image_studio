/* Real screenshot pixels and thumbnail-only backgrounds in an isolated harness. */
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-detail-pixels-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const selectedEngines = (process.env.STUDIO_TEST_BROWSERS || "chromium,webkit").split(",");

const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index,color in enumerate(((175,218,188),(224,181,190),(161,190,223))):
 width,height=((900,1200),(1200,900),(1200,1200))[index]
 image=Image.new("RGB",(width,height),color);draw=ImageDraw.Draw(image);tile=30
 for row,y in enumerate(range(height//4,height*3//4,tile)):
  for column,x in enumerate(range(width//4,width*3//4,tile)):
   draw.rectangle((x,y,x+tile-1,y+tile-1),fill=(242,244,246) if (row+column)%2 else (20,25,30))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"{marker} geometric frame {index}","model":"detail-pixel-fixture","steps":20+index,"seed":index,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata,compress_level=0);paths.append(str(file))
print(json.dumps(paths))
`;

const pixelScript = String.raw`
import json,sys
from PIL import Image
samples=json.load(sys.stdin);results=[]
for sample in samples:
 image=Image.open(sample["file"]).convert("RGB");box=sample["box"]
 cx=box["x"]+box["width"]/2;cy=box["y"]+box["height"]/2;half=min(box["width"],box["height"])*.10
 crop=image.crop((round(cx-half),round(cy-half),round(cx+half),round(cy+half)))
 data=crop.get_flattened_data() if hasattr(crop,"get_flattened_data") else crop.getdata()
 values=[sum(pixel)/3 for pixel in data];count=max(1,len(values))
 dark=sum(value<70 for value in values)/count;light=sum(value>195 for value in values)/count
 results.append({"file":sample["file"],"dark":dark,"light":light,"passed":dark>.15 and light>.15})
print(json.dumps(results))
`;

async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const value = await response.json(); return value.data || value;
}

async function seed(page, files, marker) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "detail-pixel-fixture" } }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: true }); assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok());
  }
  const result = await api(page, "post", prepared.commit_endpoint, {});
  return result.generation_id || result.generation_ids[0];
}

async function installProbe(inner) {
  await inner.evaluate(() => {
    const originalDecode = HTMLImageElement.prototype.decode;
    window.__pixelBlocked = new Set(); window.__pixelPending = [];
    HTMLImageElement.prototype.decode = async function () {
      const source = this.src;
      if (!this.className.includes("backdrop") && window.__pixelBlocked.has(source)) {
        await new Promise((resolve) => window.__pixelPending.push({ source, resolve }));
      }
      return await originalDecode.call(this);
    };
    window.__pixelBackdropEvents = [];
    new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === "attributes" && record.target.classList?.contains("detail-image-backdrop")) {
          window.__pixelBackdropEvents.push({ type: "source", source: record.target.getAttribute("src") });
        }
        for (const node of record.addedNodes || []) {
          if (node.classList?.contains("detail-backdrop-previous")) window.__pixelBackdropEvents.push({ type: "previous" });
        }
      }
    }).observe(document.getElementById("drawerBody"), { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
  });
}

async function release(inner, source) {
  await inner.evaluate((value) => {
    window.__pixelBlocked.delete(value);
    for (const pending of window.__pixelPending.filter((item) => item.source === value)) pending.resolve();
    window.__pixelPending = window.__pixelPending.filter((item) => item.source !== value);
  }, source);
}

async function mainSource(inner, source) {
  await inner.waitForFunction((value) => document.querySelector(".detail-image-frame > [data-detail-image]")?.src === value, source);
}

async function settledBackground(inner, source) {
  await inner.waitForFunction((value) => {
    const image = document.querySelector(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)");
    return image?.src === value && !document.querySelector(".detail-backdrop-previous") && !image.getAnimations().some((animation) => animation.playState === "running");
  }, source);
}

async function installLayerProbe(inner, previews) {
  await inner.evaluate((sources) => {
    const frame = document.querySelector(".detail-image-frame");
    const holder = frame.querySelector(":scope > .detail-image-background");
    const main = frame.querySelector(":scope > [data-detail-image]");
    window.__pixelLayerSamples = 0; window.__pixelLayerFailures = [];
    window.__pixelLayerSampling = true;
    window.__pixelCheckLayers = () => {
      const errors = [];
      const currentFrame = document.querySelector(".detail-image-frame");
      const currentHolder = currentFrame?.querySelector(":scope > .detail-image-background");
      const currentMain = currentFrame?.querySelector(":scope > [data-detail-image]");
      if (currentFrame !== frame || currentHolder !== holder || currentMain !== main) errors.push("persistent image layers were replaced");
      if (!currentHolder || !currentMain) return [...errors, "missing foreground or background layer"];
      const holderStyle = getComputedStyle(currentHolder);
      const mainStyle = getComputedStyle(currentMain);
      if (getComputedStyle(currentFrame).isolation !== "isolate") errors.push("image frame must isolate its stacking context");
      if (Number(holderStyle.zIndex) !== 0 || Number(mainStyle.zIndex) !== 1) errors.push("foreground must have z-index 1 above background z-index 0");
      if (currentHolder.contains(currentMain)) errors.push("foreground is inside the filtered background layer");
      if (!/blur\(24px\)/.test(holderStyle.filter) || holderStyle.transform === "none") errors.push("stationary holder must own blur and scale");
      if (currentHolder.getAnimations().length) errors.push("stationary holder must never animate");
      if (mainStyle.filter !== "none" || mainStyle.transform === "none") errors.push("foreground must remain an unfiltered independent compositor layer");
      const backgrounds = Array.from(currentFrame.querySelectorAll(".detail-image-backdrop"));
      if (!backgrounds.length) errors.push("missing background image");
      for (const background of backgrounds) {
        const style = getComputedStyle(background);
        if (background.parentElement !== currentHolder) errors.push("background image escaped its stationary holder");
        if (style.filter !== "none" || style.transform !== "none") errors.push("background image owns a filter or spatial transform");
        if (!sources.includes(background.src)) errors.push("background uses a non-thumbnail source");
        for (const animation of background.getAnimations()) {
          if (animation.effect.getKeyframes().some((keyframe) => Object.keys(keyframe).some((key) => !["offset", "computedOffset", "easing", "composite", "opacity"].includes(key)))) errors.push("background image animation changes more than opacity");
        }
      }
      const overlay = currentFrame.querySelector(".detail-swipe-overlay");
      if (overlay && (currentHolder.contains(overlay) || Number(getComputedStyle(overlay).zIndex) !== 2)) errors.push("swipe overlay must have z-index 2 outside the background holder");
      window.__pixelLayerSamples++;
      if (errors.length && window.__pixelLayerFailures.length < 30) window.__pixelLayerFailures.push({ state: currentFrame.dataset.detailSwipeState || "idle", errors });
      return errors;
    };
    const sample = () => {
      if (!window.__pixelLayerSampling) return;
      window.__pixelCheckLayers(); requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, previews);
}

async function snapshotState(inner) {
  return await inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame");
    const track = frame?.querySelector(".detail-swipe-track");
    const displacement = track ? new DOMMatrixReadOnly(getComputedStyle(track).transform).m41 : 0;
    const state = frame?.dataset.detailSwipeState || "idle";
    const stable = state === "idle" || (state === "navigating" && Math.abs(Math.abs(displacement) - frame.clientWidth) < 1);
    return { state, stable, layerErrors: window.__pixelCheckLayers() };
  });
}

async function captureDuring(page, frame, inner, label, action) {
  const samples = [];
  const box = await frame.locator(".detail-image-frame").boundingBox();
  const started = Date.now(); let done = false; let actionError;
  const work = action().catch((error) => { actionError = error; }).finally(() => { done = true; });
  while (!done || Date.now() - started < 800 || samples.length < 4) {
    const before = await snapshotState(inner);
    assert.deepEqual(before.layerErrors, [], `${label}: invalid image layers before capture`);
    const file = path.join(output, `${label}-${String(samples.length).padStart(3, "0")}.png`);
    await page.screenshot({ path: file, animations: "allow", scale: "css" });
    const after = await snapshotState(inner);
    assert.deepEqual(after.layerErrors, [], `${label}: invalid image layers after capture`);
    if (before.stable && after.stable) samples.push({ file, box });
    if (Date.now() - started > 14000) throw new Error(`${label}: action did not settle`);
  }
  await work; if (actionError) throw actionError;
  assert.ok(samples.length >= 4, `${label}: insufficient captured stable frames (${samples.length})`);
  const pixels = JSON.parse(execFileSync(python, ["-c", pixelScript], { input: JSON.stringify(samples), encoding: "utf8" }));
  assert.deepEqual(pixels.filter((item) => !item.passed), [], `${label}: missing foreground contrast in real screenshot pixels`);
  return samples.length;
}

async function clickThumbnail(frame, index) {
  await frame.locator(`[data-detail-dot="${index}"]`).evaluate((button) => button.click());
}

async function swipe(page, inner, engine) {
  const box = await inner.locator(".detail-image-frame").boundingBox();
  const start = { x: box.x + box.width * .80, y: box.y + box.height * .5 };
  const end = { x: box.x + box.width * .20, y: start.y };
  if (engine === "chromium") {
    const client = await page.context().newCDPSession(page);
    try {
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ ...start, id: 1 }] });
      for (let step = 1; step <= 6; step++) {
        await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: start.x + (end.x - start.x) * step / 6, y: start.y, id: 1 }] });
        await page.waitForTimeout(18);
      }
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } finally { await client.detach(); }
  } else {
    // Playwright has no WebKit swipe API; exercise the same handlers with TouchEvents.
    await inner.evaluate(async ({ start, end }) => {
      const frame = document.querySelector(".detail-image-frame");
      const send = (type, point) => {
        const touch = { identifier: 1, clientX: point.x, clientY: point.y, target: frame };
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [touch] }, changedTouches: { value: [touch] } });
        frame.dispatchEvent(event);
      };
      send("touchstart", start);
      for (let step = 1; step <= 6; step++) {
        send("touchmove", { x: start.x + (end.x - start.x) * step / 6, y: start.y });
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      send("touchend", end);
    }, { start, end });
  }
}

async function verify(page, frame, inner, groupId, summary, assets, name, engine) {
  const previews = summary.images.map((image) => image.thumbnail_data_url);
  const originals = assets.images.map((image) => image.data_url);
  assert.ok(previews.every((source, index) => source && source !== originals[index]));
  let releaseAssets; const assetsGate = new Promise((resolve) => { releaseAssets = resolve; });
  const pattern = "**/gallery/image/*";
  const delay = async (route) => { if (new URL(route.request().url()).searchParams.get("detail") === "original") await assetsGate; await route.continue(); };
  await page.route(pattern, delay);
  try {
    await frame.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await mainSource(inner, previews[0]); await settledBackground(inner, previews[0]);
    await installLayerProbe(inner, previews);
    await inner.evaluate((source) => window.__pixelBlocked.add(source), originals[1]);
    let count = await captureDuring(page, frame, inner, `${name}-thumbnail`, async () => {
      await clickThumbnail(frame, 1); await mainSource(inner, previews[1]); await settledBackground(inner, previews[1]);
    });
    releaseAssets();
    await inner.waitForFunction((source) => window.__pixelPending.some((item) => item.source === source), originals[1]);
    await settledBackground(inner, previews[1]);
    await inner.evaluate(() => {
      window.__pixelBackdropEvents = [];
      window.__pixelOriginalBackdrop = document.querySelector(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)");
    });
    count += await captureDuring(page, frame, inner, `${name}-original-upgrade`, async () => {
      await release(inner, originals[1]); await mainSource(inner, originals[1]);
    });
    const upgrade = await inner.evaluate(() => {
      const current = document.querySelector(".detail-image-background > .detail-image-backdrop:not(.detail-backdrop-previous)");
      return { same: current === window.__pixelOriginalBackdrop, source: current.src, events: window.__pixelBackdropEvents, animations: current.getAnimations().length };
    });
    assert.equal(upgrade.same, true); assert.equal(upgrade.source, previews[1]); assert.deepEqual(upgrade.events, []); assert.equal(upgrade.animations, 0);
    await clickThumbnail(frame, 2); await mainSource(inner, originals[2]); await settledBackground(inner, previews[2]);
    await clickThumbnail(frame, 1); await mainSource(inner, originals[1]); await settledBackground(inner, previews[1]);
    count += await captureDuring(page, frame, inner, `${name}-cached-swipe`, async () => {
      await swipe(page, inner, engine); await mainSource(inner, originals[2]);
      await inner.waitForFunction(() => !document.querySelector(".detail-swipe-overlay"));
      await settledBackground(inner, previews[2]);
    });
    assert.equal(await frame.locator(".detail-image-frame > [data-detail-image]").count(), 1);
    assert.equal(await frame.locator(".detail-swipe-overlay, .detail-backdrop-previous").count(), 0);
    const layers = await inner.evaluate(() => { window.__pixelLayerSampling = false; return { samples: window.__pixelLayerSamples, failures: window.__pixelLayerFailures }; });
    assert.ok(layers.samples > 20, `${name}: insufficient compositor structure sampling`);
    assert.deepEqual(layers.failures, [], `${name}: image compositor structure changed during switching`);
    console.log(`${name}: ${count} real screenshot ROI checks and ${layers.samples} isolated-layer checks; mixed aspect ratios, thumbnail switching, original upgrade with unchanged background, cached swipe landing passed`);
  } finally { releaseAssets(); await page.unroute(pattern, delay); }
}

(async () => {
  for (const engine of selectedEngines) {
    assert.ok(engines[engine], `Unknown Playwright engine ${engine}`);
    const browser = await engines[engine].launch({ headless: true });
    try {
      for (const test of [{ width: 390, theme: "light", dpr: 3 }, { width: 320, theme: "dark", dpr: 1 }]) {
        const name = `${engine}-${test.width}-${test.theme}-dpr${test.dpr}`;
        const marker = `${path.basename(output)}-${name}`;
        const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
        const page = await browser.newPage({ viewport: { width: test.width, height: 844 }, hasTouch: true, deviceScaleFactor: test.dpr });
        page.setDefaultTimeout(15000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
        try {
          await page.goto(base); const frame = page.frameLocator("#studio");
          await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
          const groupId = await seed(page, files, marker);
          const summary = await api(page, "get", `gallery/detail/${groupId}?assets=0`);
          const assets = await api(page, "get", `gallery/assets/${groupId}`);
          const inner = page.frames().find((item) => item.url().includes("/ui/"));
          await inner.evaluate(async (theme) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme);
          await installProbe(inner);
          await frame.locator('[data-view="gallery"]').click();
          await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
          await frame.locator(`[data-gallery-id="${groupId}"]`).waitFor();
          await verify(page, frame, inner, groupId, summary, assets, name, engine);
          assert.deepEqual(errors, []);
        } finally { await page.close(); }
      }
    } finally { await browser.close(); }
  }
  console.log(`Detail pixel screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
