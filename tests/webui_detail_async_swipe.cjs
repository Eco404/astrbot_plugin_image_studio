/* Cold previews, image decoding and manifests must not block touch navigation.
 * Run only against tests/webui_harness.py: this test imports/deletes safe fixtures.
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-async-swipe-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(40):
 image=Image.new("RGB",(280,360),(100+index*3,200-index*3,180));draw=ImageDraw.Draw(image)
 draw.rectangle((0,250,280,360),fill=(90,110+index*3,125));draw.polygon([(0,250),(110,90),(240,250)],fill=(160,110,140+index*2))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"async swipe {marker} image {index}","model":"async-swipe-model","steps":20,"seed":index,"width":280,"height":360,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
}
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(page, files, marker) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex") }));
  const batch = await api(page, "post", "imports/prepare", { items, as_group: false });
  assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  }
  const result = await api(page, "post", batch.commit_endpoint, {});
  assert.equal(result.allowed, true); return result.generation_ids;
}
async function frames(inner, count = 2) {
  await inner.evaluate(count => new Promise(resolve => {
    const tick = () => --count <= 0 ? resolve() : requestAnimationFrame(tick);
    requestAnimationFrame(tick);
  }), count);
}
async function selected(inner, item) {
  // Media may still be pending: only the navigation cursor and gesture must settle.
  await inner.waitForFunction(item => {
    const frame = document.querySelector(".detail-image-frame");
    return frame?.dataset.generationId === item.generation_id
      && frame.querySelector("[data-detail-image]")?.dataset.detailImage === String(item.image_index)
      && !frame.dataset.detailSwipeState && !frame.querySelector(".detail-swipe-overlay");
  }, item, { timeout: 1500 });
}
async function touch(inner, type, dx = 0) {
  await inner.evaluate(({ type, dx }) => {
    const frame = document.querySelector(".detail-image-frame"), rect = frame.getBoundingClientRect();
    const point = { identifier: 41, target: frame, clientX: rect.x + rect.width * .7 + dx, clientY: rect.y + Math.min(160, rect.height * .4) };
    const event = new Event(type, { bubbles: true, cancelable: true });
    const touches = type === "touchend" ? [] : [point];
    Object.defineProperties(event, { touches: { value: touches }, changedTouches: { value: [point] }, targetTouches: { value: touches } });
    frame.dispatchEvent(event);
  }, { type, dx });
}
async function motion(inner) {
  return inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame"), track = frame?.querySelector(".detail-swipe-track");
    return { phase: frame?.dataset.detailSwipeState, width: frame?.clientWidth, x: track ? new DOMMatrixReadOnly(getComputedStyle(track).transform).m41 : null, target: track ? new DOMMatrixReadOnly(track.style.transform).m41 : null };
  });
}
async function swipe(inner, target, waitForSelection = true) {
  await inner.locator("#drawerBody").evaluate(element => { element.scrollTop = 0; });
  await touch(inner, "touchstart");
  await touch(inner, "touchmove", -40); await frames(inner);
  const first = await motion(inner);
  assert.equal(first.phase, "dragging");
  assert.ok(Math.abs(first.x + 40) <= 2, `unloaded adjacent image must follow the full finger distance, not edge resistance: ${JSON.stringify(first)}`);
  await touch(inner, "touchmove", -125); await frames(inner);
  const moved = await motion(inner);
  assert.ok(Math.abs((moved.x - first.x) + 85) <= 2, `cold-image drag must remain continuous: ${JSON.stringify({ first, moved })}`);
  await touch(inner, "touchend", -125); await frames(inner);
  const released = await motion(inner);
  assert.equal(released.phase, "settling", "release must start the transition while image requests are still blocked");
  assert.ok(Math.abs(released.target + released.width) <= 2, `release must animate to the next page immediately: ${JSON.stringify(released)}`);
  if (waitForSelection) await selected(inner, target);
}
async function noSource(inner) {
  assert.equal(await inner.locator("[data-detail-image]").getAttribute("src"), null, "a pending cursor must clear the previous image without requiring a source to remain swipeable");
  assert.equal(await inner.locator(".detail-image-pending").count(), 1);
}

async function run(browserName) {
  const browser = await engines[browserName].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, deviceScaleFactor: 3 });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  const marker = `${path.basename(output)}-${browserName}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const created = [], previewGates = new Map(), manifestGates = new Map(), completed = new Set();
  let inner, failedPreviewRequests = 0;
  try {
    created.push(...await seed(page, files, marker));
    const sequence = (await api(page, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    await page.goto(base); inner = page.frames().find(frame => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.waitForFunction(marker => {
      const cards = Array.from(document.querySelectorAll("[data-gallery-id]"));
      return cards.length > 0 && cards.every(card => card.textContent.includes(marker));
    }, marker);
    const visible = await inner.locator("[data-gallery-id]").evaluateAll(cards => cards.map(card => card.dataset.galleryId));
    const edge = sequence.findIndex(item => item.generation_id === visible.at(-1));
    const [A, B, C, D, E, F] = sequence.slice(edge, edge + 6);
    assert.ok(F, "fixtures must include five cold groups beyond the visible gallery page");
    assert.ok([B, C, D, E, F].every(item => !visible.includes(item.generation_id)), "cold targets must not have loaded gallery cover thumbnails");
    for (const item of [B, C]) previewGates.set(item.image_id, gate());
    for (const item of [B, C, D]) manifestGates.set(item.generation_id, gate());
    const ePreview = (await api(page, "get", `gallery/image/${E.image_id}?detail=preview`)).data_url;
    // Simulate expensive decoding separately from the network. The target can
    // finish downloading while the app's decode promise remains unresolved.
    await inner.evaluate(source => {
      const decode = HTMLImageElement.prototype.decode;
      window.__asyncSwipeDecodeCount = 0;
      window.__asyncSwipeDecodeGate = new Promise(resolve => { window.__releaseAsyncSwipeDecode = resolve; });
      HTMLImageElement.prototype.decode = function () {
        const result = decode.call(this);
        if (this.src !== source) return result;
        window.__asyncSwipeDecodeCount++;
        return result.then(value => window.__asyncSwipeDecodeGate.then(() => value));
      };
    }, ePreview);
    await page.route("**/gallery/detail/*", async route => {
      const id = new URL(route.request().url()).pathname.split("/").at(-1);
      if (manifestGates.has(id)) await manifestGates.get(id).promise;
      const response = await route.fetch();
      await route.fulfill({ response }); completed.add(`manifest:${id}`);
    });
    await page.route("**/gallery/image/*", async route => {
      const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1), kind = url.searchParams.get("detail");
      if (kind === "original" && id !== A.image_id) {
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试：原图暂时不可用" }) }); return;
      }
      if (kind === "preview" && id === D.image_id) {
        failedPreviewRequests++;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试：预览暂时不可用" }) }); return;
      }
      if (kind === "preview" && previewGates.has(id)) await previewGates.get(id).promise;
      const response = await route.fetch();
      await route.fulfill({ response }); completed.add(`${kind}:${id}`);
    });
    await inner.locator(`[data-gallery-id="${A.generation_id}"] .gallery-info`).click();
    await selected(inner, A);
    await inner.waitForFunction(() => {
      const image = document.querySelector("[data-detail-image]");
      return image?.complete && image.naturalWidth > 1;
    });
    await frames(inner, 5);

    await swipe(inner, B, false);
    // Start the next touch before the previous release animation finishes.
    // Waiting for selected()/idle here would miss the real rapid-swipe bug.
    const beforeRepeat = await motion(inner);
    assert.notEqual(beforeRepeat.phase, undefined, "second gesture must overlap the first transition");
    await touch(inner, "touchstart");
    await touch(inner, "touchmove", -125); await frames(inner);
    assert.equal((await motion(inner)).phase, "dragging", "a cold cross-group transition must accept the next touch before settling");
    await touch(inner, "touchend", -125);
    await selected(inner, C); await noSource(inner);
    assert.equal(await inner.locator(".detail-manifest-loading").count(), 1, "multiple pending manifests must not keep navigation locked");
    await page.screenshot({ path: path.join(output, `${browserName}-cold-current.png`) });

    // A prior target's late image/metadata must not overwrite the newer cursor.
    previewGates.get(B.image_id).release(); manifestGates.get(B.generation_id).release();
    for (let attempt = 0; attempt < 100 && (!completed.has(`preview:${B.image_id}`) || !completed.has(`manifest:${B.generation_id}`)); attempt++) await page.waitForTimeout(20);
    assert.ok(completed.has(`preview:${B.image_id}`) && completed.has(`manifest:${B.generation_id}`), "stale responses must actually complete for the identity check");
    await frames(inner, 8); await selected(inner, C); await noSource(inner);

    await swipe(inner, D);
    assert.ok(failedPreviewRequests > 0, "the unavailable preview path must have been exercised");
    await noSource(inner);
    await swipe(inner, E);
    await inner.waitForFunction(() => window.__asyncSwipeDecodeCount > 0);
    await swipe(inner, F);
    await inner.waitForFunction(() => {
      const image = document.querySelector("[data-detail-image]");
      return image?.complete && image.naturalWidth > 1;
    });
    const finalSource = await inner.locator("[data-detail-image]").getAttribute("src");
    await inner.evaluate(() => window.__releaseAsyncSwipeDecode());
    previewGates.get(C.image_id).release(); manifestGates.get(C.generation_id).release(); manifestGates.get(D.generation_id).release();
    await frames(inner, 20); await selected(inner, F);
    assert.equal(await inner.locator("[data-detail-image]").getAttribute("src"), finalSource, "late decoding/preview/manifest responses must not roll back the image");
    assert.deepEqual(await inner.locator("[data-gallery-id]").evaluateAll(cards => cards.map(card => card.dataset.galleryId)), visible, "cross-page details must not reload the obscured gallery");
    assert.deepEqual(errors, []);
    console.log(`${browserName} mobile: cold previews follow the finger, release animates without I/O, source-less cursors remain swipeable, preview failure and delayed decoding do not trap navigation, stale results do not overwrite the current image`);
  } finally {
    for (const item of [...previewGates.values(), ...manifestGates.values()]) item.release();
    if (inner) await inner.evaluate(() => window.__releaseAsyncSwipeDecode?.()).catch(() => {});
    await page.unrouteAll({ behavior: "wait" });
    if (inner) await inner.locator("#closeDrawer").evaluate(button => button.click()).catch(() => {});
    if (created.length) await api(page, "post", "gallery/delete", { ids: created });
    await page.close(); await browser.close();
  }
}

(async () => {
  for (const engine of (process.env.STUDIO_TEST_ENGINES || "chromium,webkit").split(",")) await run(engine);
  console.log(`Async swipe screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
