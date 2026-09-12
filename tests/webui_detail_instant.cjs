/* Cached cross-group previews must never wait for group metadata. Isolated harness only. */
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-instant-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(10):
 image=Image.new("RGB",(300,380),(110+index*10,200-index*9,180));draw=ImageDraw.Draw(image)
 draw.rectangle((0,260,300,380),fill=(90,110+index*9,125));draw.polygon([(0,260),(110,90),(260,260)],fill=(160,110,140+index*9))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"instant {marker} image {index}","model":"instant-model","steps":20+index,"seed":index,"width":300,"height":380,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(page, files, marker) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "instant-model" } }));
  const batch = await api(page, "post", "imports/prepare", { items, as_group: files.length > 1 }); assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  }
  const result = await api(page, "post", batch.commit_endpoint, {}); assert.equal(result.allowed, true); return result.generation_ids[0];
}
async function selected(inner, generation, index, source) {
  await inner.waitForFunction(({ generation, index, source }) => {
    const frame = document.querySelector(".detail-image-frame"), image = frame?.querySelector("[data-detail-image]");
    return frame?.dataset.generationId === generation && frame.getAttribute("aria-busy") !== "true"
      && image?.dataset.detailImage === String(index) && image.complete && image.naturalWidth > 1
      && (!source || image.src === source) && !frame.dataset.detailSwipeState;
  }, { generation, index, source }, { timeout: 5000 });
}
async function frames(inner, count = 6) {
  await inner.evaluate(count => new Promise(resolve => {
    const tick = () => --count <= 0 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick);
  }), count);
}
async function touch(inner, type, dx = 0) {
  await inner.evaluate(({ type, dx }) => {
    const frame = document.querySelector(".detail-image-frame"), rect = frame.getBoundingClientRect();
    const point = { identifier: 41, target: frame, clientX: rect.x + rect.width * .5 + dx, clientY: rect.y + Math.min(160, rect.height * .4) };
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [point] }, changedTouches: { value: [point] }, targetTouches: { value: type === "touchend" ? [] : [point] } });
    frame.dispatchEvent(event);
  }, { type, dx });
}
async function navigate(inner, direction, mobile) {
  if (!mobile) { await inner.locator(`[data-detail-nav="${direction}"]`).click(); return; }
  await inner.locator("#drawerBody").evaluate(element => { element.scrollTop = 0; });
  await touch(inner, "touchstart"); await touch(inner, "touchmove", -direction * 75);
  await touch(inner, "touchmove", -direction * 150); await touch(inner, "touchend", -direction * 150);
}
async function pending(inner) {
  assert.equal(await inner.locator(".detail-manifest-loading").count(), 1, "metadata must have its own pending placeholder");
  assert.equal(await inner.locator(".detail-parameter-row").count(), 0, "pending group must not show previous group parameters");
  assert.equal(await inner.locator("#detailCopy").isDisabled(), true, "metadata-dependent actions must stay disabled");
}

async function run(browserName, width) {
  const mobile = width <= 540, browser = await engines[browserName].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 844 }, hasTouch: mobile, deviceScaleFactor: mobile ? 3 : 1 });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  const marker = `${path.basename(output)}-${browserName}-${width}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const created = [], manifestGates = new Map(), requests = [], originalGate = gate(), coldPreview = gate();
  const count = (kind, id) => requests.filter(item => item.kind === kind && item.id === id).length;
  let inner;
  page.on("request", request => {
    const url = new URL(request.url()), match = url.pathname.match(/\/gallery\/(detail|image-info|image)\/([^/]+)$/);
    if (match) requests.push({ kind: match[1] === "image" ? url.searchParams.get("detail") : match[1], id: decodeURIComponent(match[2]) });
  });
  try {
    // Imported newest-first groups A(3), B(3), C(3), D(1); all covers fit one page.
    const D = await seed(page, files.slice(9), `${marker}-d`); created.push(D);
    const C = await seed(page, files.slice(6, 9), `${marker}-c`); created.push(C);
    const B = await seed(page, files.slice(3, 6), `${marker}-b`); created.push(B);
    const A = await seed(page, files.slice(0, 3), `${marker}-a`); created.push(A);
    const manifests = Object.fromEntries(await Promise.all(created.map(async id => [id, await api(page, "get", `gallery/detail/${id}?light=1`)])));
    const sequence = (await api(page, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    assert.deepEqual([...new Set(sequence.map(item => item.generation_id))], [A, B, C, D]);
    const aLast = manifests[A].images[2].id, cFirst = manifests[C].images[0].id, cLast = manifests[C].images[2].id, dFirst = manifests[D].images[0].id;
    const expected = Object.fromEntries(await Promise.all([aLast, cFirst, cLast, dFirst].map(async id => [id, (await api(page, "get", `gallery/image/${id}?detail=preview`)).data_url])));
    for (const id of [A, C, D]) manifestGates.set(id, gate());
    await page.route("**/gallery/detail/*", async route => {
      const id = new URL(route.request().url()).pathname.split("/").at(-1);
      if (manifestGates.has(id)) await manifestGates.get(id).promise;
      try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
    });
    await page.route("**/gallery/image/*", async route => {
      const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1);
      if (url.searchParams.get("detail") === "original") await originalGate.promise;
      else if (id === cLast) await coldPreview.promise;
      try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
    });
    await page.goto(base); inner = page.frames().find(frame => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${B}"] .gallery-info`).click(); await selected(inner, B, 0);
    await inner.locator("#detailCopy:not(:disabled)").waitFor();
    if (!count("preview", aLast)) await page.waitForRequest(request => request.url().includes(`/gallery/image/${aLast}?`));
    await frames(inner, 10);
    assert.equal(count("preview", aLast), 1, "first image must warm the previous group's last image even while its manifest is blocked");
    assert.equal(count("preview", manifests[A].images[0].id), 0, "cached gallery cover must not be reread to fake the previous boundary");
    assert.equal(count("original", aLast), 0); assert.equal(count("image-info", aLast), 0, "boundary warming must not fetch parameters");

    await navigate(inner, -1, mobile); await selected(inner, A, 2, expected[aLast]); await pending(inner);
    assert.equal(count("preview", aLast), 1, "backward handoff must reuse the warmed last image");
    await navigate(inner, 1, mobile); await selected(inner, B, 0);
    await inner.locator('[data-detail-dot="2"]').click(); await selected(inner, B, 2);
    await frames(inner, 8);
    assert.equal(count("preview", cFirst), 0, "last image must reuse the next group's gallery cover without a new preview request");
    assert.equal(count("original", cFirst), 0); assert.equal(count("image-info", cFirst), 0);
    await navigate(inner, 1, mobile); await selected(inner, C, 0, expected[cFirst]); await pending(inner);
    assert.equal(count("preview", cFirst), 0, "cached next-group handoff must remain entirely independent of preview HTTP");
    assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), 3, "browse sequence must supply the pending group filmstrip");

    // A not-yet-loaded item in the pending group's filmstrip also cannot wait for metadata.
    await inner.locator('[data-detail-dot="2"]').click(); coldPreview.release();
    await selected(inner, C, 2, expected[cLast]); await pending(inner);
    assert.equal(count("preview", cLast), 1, "a cold pending filmstrip selection should share one preview request");
    await navigate(inner, 1, mobile); await selected(inner, D, 0, expected[dFirst]); await pending(inner);
    assert.equal(count("preview", dFirst), 0, "another cached group must remain navigable with multiple manifests pending");
    await navigate(inner, -1, mobile); await selected(inner, C, 2, expected[cLast]); await pending(inner);
    assert.equal(count("preview", cLast), 1);

    // Delayed unrelated manifests must not replace the visible group or image.
    manifestGates.get(A).release(); manifestGates.get(D).release();
    await frames(inner, 15); await selected(inner, C, 2, expected[cLast]); await pending(inner);
    manifestGates.get(C).release(); originalGate.release();
    await inner.locator(".detail-manifest-loading").waitFor({ state: "detached" });
    await selected(inner, C, 2); await inner.locator("#detailCopy:not(:disabled)").waitFor();
    assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), 3);
    assert.equal(await inner.locator('[data-detail-dot="2"]').getAttribute("aria-current"), "true");
    const original = (await api(page, "get", `gallery/image/${cLast}?detail=original`)).data_url;
    await selected(inner, C, 2, original); await frames(inner, 12); await selected(inner, C, 2, original);
    assert.notEqual(original, expected[cLast], "fixture must distinguish preview and original to catch a late preview rollback");
    await page.screenshot({ path: path.join(output, `${browserName}-${width}.png`) });
    assert.deepEqual(errors, []);
    console.log(`${browserName} ${width}: cached cross-group handoff, thumbnail-only boundary warming, pending filmstrip, continued navigation and late-manifest identity passed`);
  } finally {
    originalGate.release(); coldPreview.release(); for (const item of manifestGates.values()) item.release();
    if (inner) await inner.locator("#closeDrawer").evaluate(button => button.click()).catch(() => {});
    if (created.length) await api(page, "post", "gallery/delete", { ids: created });
    await page.close(); await browser.close();
  }
}

(async () => {
  for (const [engine, width] of [["chromium", 1440], ["chromium", 390], ["webkit", 390]]) await run(engine, width);
  console.log(`Instant-preview screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
