/* Run only against tests/webui_harness.py; fixtures are generated landscapes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-detail-lazy-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const imageCount = 30;
let serial = 0;

const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(30):
 width,height=((480,640),(640,480),(560,560))[index%3]
 image=Image.new("RGB",(width,height),(162+index,202,219-index));draw=ImageDraw.Draw(image)
 draw.rectangle((0,height*.64,width,height),fill=(90+index,144,125));draw.polygon([(0,height*.72),(width*.35,height*.24),(width*.76,height*.72)],fill=(110,134+index,159));draw.ellipse((width*.73,height*.1,width*.9,height*.24),fill=(235,239,215))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"safe landscape {marker} image {index}","model":"detail-lazy-model","uc":"blur","steps":24,"seed":1000+index,"width":width,"height":height,"request_type":"PromptGenerateRequest","fixture_extra":{"description":"deferred raw field "*160}}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

function gate() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}
async function continueRoute(route) {
  try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
}
async function body(response) { const value = await response.json(); return value.data || value; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data === undefined ? {} : { data });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  return await body(response);
}
async function seed(page, files) {
  const items = files.map((file) => ({ client_id: `lazy_${Date.now().toString(36)}_${++serial}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "detail-lazy-model" } }));
  const prepared = await api(page, "post", "imports/prepare", { items, as_group: true });
  assert.equal(prepared.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${prepared.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.notEqual((await body(response)).allowed, false);
  }
  const committed = await api(page, "post", prepared.commit_endpoint, {});
  assert.equal(committed.allowed, true); assert.equal(committed.generation_ids.length, 1);
  return committed.generation_ids[0];
}
function idsFor(requests, kind) {
  return [...new Set(requests.filter((item) => item.kind === kind).map((item) => item.id))];
}
function assertIds(actual, expected, message) {
  assert.deepEqual([...actual].sort(), [...expected].sort(), message);
}
async function frames(inner, count = 4) {
  await inner.evaluate((remaining) => new Promise((resolve) => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick);
    requestAnimationFrame(tick);
  }), count);
}
async function selected(inner, index, marker) {
  await inner.locator(`[data-detail-dot="${index}"][aria-current="true"]`).waitFor();
  await inner.locator(`[data-detail-image="${index}"]`).waitFor();
  await inner.locator(".detail-parameter-row").filter({ hasText: `safe landscape ${marker} image ${index}` }).first().waitFor();
}
async function originalReady(inner, source) {
  await inner.waitForFunction((expected) => {
    const image = document.querySelector(".detail-image-frame > [data-detail-image]");
    return image?.src === expected && image.complete && image.naturalWidth > 1;
  }, source);
  await inner.locator("#detailUseReference:not(:disabled)").waitFor();
}
async function nearFilmstrip(inner) {
  return await inner.evaluate(() => {
    const strip = document.querySelector(".detail-filmstrip");
    const bounds = strip.getBoundingClientRect();
    return [...strip.querySelectorAll("[data-detail-dot]")].filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.right >= bounds.left - 160 && rect.left <= bounds.right + 160;
    }).map((item) => Number(item.dataset.detailDot));
  });
}
async function capture(page, inner, name) {
  await frames(inner);
  const layout = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(layout.scroll <= layout.width + 1, `${name}: horizontal overflow ${JSON.stringify(layout)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function run(engine, width) {
  const name = `${engine}-${width}`;
  const marker = `${path.basename(output)}-${name}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const browser = await engines[engine].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  const requests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    const match = url.pathname.match(/\/gallery\/(detail|image-info|image|assets)\/([^/]+)$/);
    if (!match) return;
    requests.push({ kind: match[1] === "image" ? url.searchParams.get("detail") || "original" : match[1], id: decodeURIComponent(match[2]), light: url.searchParams.get("light"), url: request.url() });
  });
  const manifestGate = gate(); const originalGate = gate(); const manifestStarted = gate();
  let groupId;
  try {
    groupId = await seed(page, files);
    const manifest = await api(page, "get", `gallery/detail/${groupId}?light=1`);
    assert.equal(manifest.lightweight, true);
    assert.equal(manifest.images.length, imageCount);
    assert.ok(manifest.images.every((image) => image.id && image.width > 0 && image.height > 0));
    assert.ok(manifest.images.every((image) => !image.data_url && !image.thumbnail_data_url && !image.metadata && !image.supplemental), "light manifest must contain no image bytes or per-image metadata");
    const ids = manifest.images.map((image) => image.id);
    const currentOriginal = (await api(page, "get", `gallery/image/${ids[0]}?detail=original`)).data_url;
    const farIndex = 26;
    const farOriginal = (await api(page, "get", `gallery/image/${ids[farIndex]}?detail=original`)).data_url;
    const manifestRoute = `**/gallery/detail/${groupId}?*`;
    await page.route(manifestRoute, async (route) => {
      manifestStarted.release(); await manifestGate.promise; await continueRoute(route);
    });
    await page.route("**/gallery/image/*", async (route) => {
      if (new URL(route.request().url()).searchParams.get("detail") === "original") await originalGate.promise;
      await continueRoute(route);
    });
    await page.goto(base);
    const inner = page.frames().find((frame) => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.evaluate(() => {
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__testViewer = this; } };
    });
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${groupId}"] .gallery-info`).click();
    await manifestStarted.promise;
    await inner.locator("#detailDrawer.is-open").waitFor();
    assert.equal(await inner.locator("#detailDrawer").getAttribute("aria-hidden"), "false");
    assert.ok((await inner.locator("#drawerBody").boundingBox()).height > 100, "detail shell must be mounted before the held manifest resolves");
    assert.match(await inner.locator("#drawerBody").innerText(), /正在读取|加载/);
    assertIds(idsFor(requests, "original"), [], "opening the shell cannot fetch any original before the manifest");
    assertIds(idsFor(requests, "image-info"), [], "opening the shell cannot fetch metadata before the manifest");
    await capture(page, inner, `${name}-manifest-pending`);
    manifestGate.release();
    await selected(inner, 0, marker);
    assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), imageCount);
    await frames(inner, 12);
    const initialNear = await nearFilmstrip(inner);
    const initialPreviews = idsFor(requests, "preview");
    const allowedInitial = new Set([...initialNear, 0, 1].map((index) => ids[index]));
    assert.ok(initialPreviews.length > 0 && initialPreviews.length < imageCount, `${name}: opening requested ${initialPreviews.length} previews for ${imageCount} images`);
    assert.ok(initialPreviews.every((id) => allowedInitial.has(id)), "initial previews must belong to adjacent images or nearby filmstrip thumbnails");
    assertIds(idsFor(requests, "original"), [ids[0]], "normal detail open may request only the selected original");
    assertIds(idsFor(requests, "image-info"), [ids[0]], "filmstrip and neighbor prefetch must not request full metadata");
    assert.equal(await inner.locator("#detailUseReference").isDisabled(), false, "reference action stages the original server-side without waiting for the browser download");
    const raw = inner.locator(".raw-metadata");
    await raw.waitFor({ state: "attached" });
    assert.equal(await raw.getAttribute("open"), null);
    assert.equal(await raw.locator(".detail-parameter-row").count(), 0, "collapsed raw metadata must not eagerly render parameter rows");
    originalGate.release(); await originalReady(inner, currentOriginal);
    await capture(page, inner, `${name}-initial-current`);

    const beforeFar = requests.length;
    await inner.locator(`[data-detail-dot="${farIndex}"]`).evaluate((button) => button.click());
    await selected(inner, farIndex, marker); await originalReady(inner, farOriginal); await frames(inner, 12);
    const farRequests = requests.slice(beforeFar);
    const farNear = await nearFilmstrip(inner);
    const allowedFar = new Set([...initialNear, ...farNear, farIndex - 1, farIndex, farIndex + 1].map((index) => ids[index]));
    assertIds(idsFor(farRequests, "original"), [ids[farIndex]], "jumping to a distant thumbnail may load only that original");
    assertIds(idsFor(farRequests, "image-info"), [ids[farIndex]], "jumping to a distant thumbnail may load only that image's metadata");
    assert.ok(idsFor(farRequests, "preview").every((id) => allowedFar.has(id)), "far selection may load adjacent previews and the newly visible filmstrip only");
    assert.ok(idsFor(farRequests, "preview").length <= farNear.length + 2, "far selection must stay within its nearby filmstrip and adjacent-preview budget");
    await capture(page, inner, `${name}-far-current`);

    // Metadata expands without another network request, using only the current image's data.
    const beforeRaw = requests.length;
    await inner.locator(".raw-metadata > summary").click();
    const rawField = inner.locator(".raw-metadata .metadata-raw-field").filter({ hasText: "Comment" });
    await rawField.locator("summary").click();
    await rawField.locator(".detail-parameter-row").first().waitFor();
    assert.ok((await rawField.innerText()).includes(`image ${farIndex}`));
    assertIds(idsFor(requests.slice(beforeRaw), "image-info"), [], "expanding current raw metadata must reuse the current metadata response");
    await inner.locator(".raw-metadata > summary").click();

    await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; });
    const viewerIndex = farIndex + (width < 600 ? 2 : 1);
    const beforeViewer = requests.length;
    await inner.locator("[data-detail-image]").click();
    if (width < 600) {
      await inner.waitForFunction(() => window.__testViewer?.opener.isOpen);
      await inner.evaluate(() => window.__testViewer.goTo(window.__testViewer.currIndex + 2));
      await inner.waitForFunction((index) => window.__testViewer.currSlide.data.image_index === index, viewerIndex);
      assert.equal(await inner.locator('[data-detail-dot][aria-current="true"]').getAttribute("data-detail-dot"), String(farIndex), "mobile viewer must defer detail selection until close");
      assertIds(idsFor(requests.slice(beforeViewer), "image-info"), [], "mobile viewer neighbor loading must not fetch full metadata");
      await inner.evaluate(() => window.__testViewer.close());
      await inner.locator(".pswp--open").waitFor({ state: "detached" });
    } else {
      await inner.locator("#imagePreview:not(.is-hidden)").waitFor();
      await inner.locator("#imagePreviewNext").click();
      await inner.locator("#closeImagePreview").click();
    }
    await selected(inner, viewerIndex, marker);
    await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    assertIds(idsFor(requests.slice(beforeViewer), "image-info"), [ids[viewerIndex]], "return from the viewer must load the landed image's metadata only");
    assert.ok(idsFor(requests.slice(beforeViewer), "original").every((id) => [ids[farIndex], ids[farIndex + 1], ids[viewerIndex]].includes(id)), "viewer navigation must not load unrelated originals");
    await capture(page, inner, `${name}-viewer-return`);
    for (const selector of ["#detailFavorite", "#detailImportEdit", "#detailReproduce", "#detailCopy", "#detailDelete", "#detailUseReference"]) assert.equal(await inner.locator(selector).isEnabled(), true, `${selector} must remain available for the current image`);
    assertIds(idsFor(requests, "assets"), [], "normal detail open, navigation and viewer return must never fetch a whole group's assets");
    assert.ok(requests.filter((item) => item.kind === "detail").every((item) => item.light === "1"), "navigation manifests must use light=1");
    assert.deepEqual(errors, [], `${name}: browser exceptions`);
    console.log(`${name}: shell-before-manifest, current-only originals/metadata, bounded ${initialPreviews.length}-thumbnail opening, far selection, lazy raw metadata and viewer return passed`);
  } finally {
    manifestGate.release(); originalGate.release();
    if (groupId) await api(page, "post", "gallery/delete", { ids: [groupId] });
    await page.close(); await browser.close();
  }
}

async function runReferences() {
  const marker = `${path.basename(output)}-retained-references`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const browser = await engines.chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  const referenceRequests = []; const deleteRequests = []; const assetsRequests = []; const referenceGeometry = [];
  page.on("request", (request) => {
    if (request.url().includes("/gallery/reference-image/")) referenceRequests.push(request.url().split("/").pop());
    if (request.url().endsWith("/gallery/reference/delete")) deleteRequests.push(request);
    if (request.url().includes("/gallery/assets/")) assetsRequests.push(request.url());
  });
  const referenceGates = [gate(), gate()]; const referenceStarted = [gate(), gate()];
  let generationId;
  try {
    const staged = [];
    for (const file of files.slice(0, 2)) {
      const response = await page.request.post(`${apiRoot}/studio/reference/upload`, { multipart: { file: { name: path.basename(file), mimeType: "image/png", buffer: fs.readFileSync(file) } } });
      assert.ok(response.ok()); staged.push((await body(response)).id);
    }
    const generated = await api(page, "post", "studio/generate", { mode: "img2img", provider_id: "natural", model_ref: "natural:studio-image", model: "studio-image", prompt: `${marker}. ${"Mountain landscape in daylight. ".repeat(40)}`, size: "1024x1024", count: 1, parameters: {}, reference_ids: staged });
    generationId = generated.generation_id; assert.ok(generationId);
    const manifest = await api(page, "get", `gallery/detail/${generationId}?light=1`);
    assert.equal(manifest.references.length, 2);
    assert.ok(manifest.references.every((reference) => reference.available && !reference.data_url));
    const references = manifest.references;
    for (let index = 0; index < references.length; index++) {
      await page.route(`**/gallery/reference-image/${references[index].id}`, async (route) => {
        const frame = page.frames().find((item) => item.url().includes("/ui/"));
        referenceGeometry.push(await frame.evaluate((id) => ({ viewport: document.getElementById("drawerBody").getBoundingClientRect().toJSON(), reference: document.querySelector(`[data-reference-load="${id}"]`)?.getBoundingClientRect().toJSON(), metadataRows: document.querySelectorAll(".detail-parameter-row").length }), references[index].id));
        referenceStarted[index].release(); await referenceGates[index].promise; await continueRoute(route);
      });
    }
    await page.goto(base);
    const inner = page.frames().find((frame) => frame.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${generationId}"] .gallery-info`).click();
    await inner.locator("#detailCopy:not(:disabled)").waitFor();
    await inner.locator("#detailUseReference:not(:disabled)").waitFor();
    await frames(inner, 12);
    assert.equal(await inner.locator("[data-reference-load]").count(), 2);
    assert.deepEqual(referenceRequests, [], `retained reference bytes must not load while the references are below the visible drawer: ${JSON.stringify(referenceGeometry)}`);
    await inner.locator("[data-reference-load]").first().evaluate((element) => element.scrollIntoView({ block: "end" }));
    await Promise.all(referenceStarted.map((item) => item.promise));
    assert.equal(await inner.locator("[data-detail-reference]").count(), 0, "held references must keep their placeholders");
    referenceGates[0].release();
    const firstReference = inner.locator(`[data-detail-reference="${references[0].id}"]`);
    await firstReference.waitFor();
    await inner.locator(`[data-reference-delete="${references[0].id}"]`).waitFor();
    // Loading the second reference rebinds the surrounding reference actions.
    referenceGates[1].release();
    await inner.locator(`[data-detail-reference="${references[1].id}"]`).waitFor();
    await inner.waitForFunction(() => [...document.querySelectorAll("[data-detail-reference]")].every((image) => image.complete && image.naturalWidth > 1));
    assert.equal(await inner.locator("[data-reference-delete]").count(), 2);
    assertIds(referenceRequests, references.map((reference) => reference.id), "each visible reference must hydrate once");
    await capture(page, inner, "chromium-390-retained-references");

    await firstReference.click();
    await inner.locator("#imagePreview:not(.is-hidden)").waitFor();
    assert.equal(await inner.locator("#previewImage").getAttribute("src"), await firstReference.getAttribute("src"), "hydrated reference must open its own preview");
    await inner.locator("#closeImagePreview").click();
    await inner.evaluate(() => {
      window.__referenceConfirmShows = 0;
      const classes = document.getElementById("confirmDialog").classList;
      const remove = classes.remove.bind(classes);
      classes.remove = (...tokens) => { if (tokens.includes("is-hidden")) window.__referenceConfirmShows++; remove(...tokens); };
    });
    await inner.locator(`[data-reference-delete="${references[0].id}"]`).click();
    await inner.locator("#confirmDialog:not(.is-hidden)").waitFor();
    assert.equal(await inner.evaluate(() => window.__referenceConfirmShows), 1, "rehydration must not attach duplicate confirmation handlers");
    assert.equal(deleteRequests.length, 0, "reference deletion must wait for confirmation");
    const deleted = page.waitForResponse((response) => response.url().endsWith("/gallery/reference/delete"));
    await inner.locator("#confirmAccept").click();
    assert.ok((await deleted).ok());
    await inner.locator(".detail-reference").filter({ hasText: "参考图已删除" }).waitFor({ state: "attached" });
    await frames(inner, 6);
    assert.equal(deleteRequests.length, 1, "one confirmation must send exactly one reference-delete request");
    assert.equal(await inner.evaluate(() => window.__referenceConfirmShows), 1);
    const refreshed = await api(page, "get", `gallery/detail/${generationId}?light=1`);
    assert.equal(refreshed.references.find((reference) => reference.id === references[0].id).available, false);
    assert.equal(refreshed.references.find((reference) => reference.id === references[1].id).available, true);
    assert.deepEqual(assetsRequests, [], "reference hydration, preview and deletion must never fetch whole-group assets");
    assert.deepEqual(errors, []);
    console.log("chromium-390 references: offscreen deferral, independent hydration, preview, single confirmation/delete handler and retained sibling passed");
  } finally {
    referenceGates.forEach((item) => item.release());
    if (generationId) await api(page, "post", "gallery/delete", { ids: [generationId] });
    await page.close(); await browser.close();
  }
}

(async () => {
  if (process.env.STUDIO_DETAIL_SCENARIO === "references") {
    await runReferences(); console.log(`Lazy reference screenshots: ${output}`); return;
  }
  const cases = process.env.STUDIO_DETAIL_CASES ? process.env.STUDIO_DETAIL_CASES.split(",").map((value) => { const [engine, width] = value.split(":"); return [engine, Number(width)]; }) : [["chromium", 1440], ["chromium", 390], ["webkit", 1440], ["webkit", 390]];
  for (const [engine, width] of cases) await run(engine, width);
  await runReferences();
  console.log(`Lazy detail screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
