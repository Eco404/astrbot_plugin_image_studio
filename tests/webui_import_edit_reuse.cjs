/* Run only against a fresh tests/webui_harness.py instance, never a deployment. */
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const root = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio/`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, message) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) { if (await check()) return; await pause(40); }
  throw new Error(message);
}
async function json(response) { assert.ok(response.ok(), await response.text()); return response.json(); }
async function get(page, endpoint) { return json(await page.request.get(root + endpoint)); }
async function seed(page, marker) {
  const encoded = JSON.parse(execFileSync(python, ["-c", String.raw`
import base64,io,json,sys
from PIL import Image,PngImagePlugin
out=[]
for i in range(3):
 info=PngImagePlugin.PngInfo(); info.add_text("parameters",f"{sys.argv[1]} original {i}\nNegative prompt: blur\nSteps: 20, Sampler: Euler, CFG scale: 7, Seed: {i}, Size: 96x64, Model: reuse-model"); info.add_text("SnapshotNotes","x"*120000)
 image=Image.new("RGB",(96,64),(i*60,120,180)); buffer=io.BytesIO(); image.save(buffer,"PNG",pnginfo=info); out.append(base64.b64encode(buffer.getvalue()).decode())
print(json.dumps(out))
`, marker], { encoding: "utf8", maxBuffer: 3 * 1024 * 1024 }));
  const buffers = encoded.map((item) => Buffer.from(item, "base64"));
  const items = buffers.map((buffer, index) => ({ client_id: `reuse_${index}`, sha256: crypto.createHash("sha256").update(buffer).digest("hex"), filename: `reuse-${index}.png`, overrides: { prompt: `${marker} original ${index}`, model: "reuse-model" } }));
  const prepared = await json(await page.request.post(root + "imports/prepare", { data: { as_group: true, items } }));
  for (let index = 0; index < buffers.length; index++) await json(await page.request.post(root + prepared.items[index].upload_endpoint, { multipart: { file: { name: items[index].filename, mimeType: "image/png", buffer: buffers[index] } } }));
  await json(await page.request.post(root + prepared.commit_endpoint, { data: {} }));
  return (await get(page, "gallery/list?query=" + encodeURIComponent(marker))).items[0].id;
}

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(20000);
  const marker = `reuse-editor-${name}-${Date.now()}`;
  const id = await seed(page, marker);
  const requests = [], errors = [];
  let holdImageId = "", releaseRead, holdStarted = false;
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route(`**/gallery/import-edit/${id}*`, async (route) => {
    const request = route.request(), params = new URL(request.url()).searchParams;
    if (request.method() === "GET" && params.has("image_id") && !params.has("output_node_id")) {
      requests.push({ imageId: params.get("image_id"), revision: params.get("item_revision"), preview: params.get("include_preview") });
      if (params.get("image_id") === holdImageId && !holdStarted) {
        holdStarted = true;
        await new Promise((resolve) => { releaseRead = resolve; });
      }
    }
    return route.continue();
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Enter");
    await frame.locator(`[data-gallery-id="${id}"] .gallery-info`).click();
    const grid = frame.locator("#importEditGrid"), cards = grid.locator(".import-card");
    async function open() { await frame.locator("#detailImportEdit:not(:disabled)").click(); await until(() => cards.count().then((n) => n === 3), "missing editor manifest"); }
    async function reveal(index) {
      const card = cards.nth(index);
      await card.evaluate((node) => node.scrollIntoView({ block: "start" }));
      await card.locator('[data-import-field="prompt"]').waitFor({ state: "attached" });
      return card;
    }
    async function close() { await frame.locator("#importEditCancel").click(); await frame.locator("#studioModalRoot").waitFor({ state: "hidden" }); }
    await open();
    for (let i = 0; i < 3; i++) await reveal(i);
    assert.equal(requests.length, 3);
    const manifest = await get(page, `gallery/import-edit/${id}?light=1`);
    const firstId = manifest.items[0].image_id;
    assert.equal(requests.find((item) => item.imageId === firstId).preview, "0", "gallery/detail thumbnail should be reused by editor hydration");
    await cards.first().locator('[data-import-field="prompt"]').fill("cancelled draft must not enter server snapshot");
    await close();
    requests.length = 0;
    await open(); for (let i = 0; i < 3; i++) await reveal(i);
    assert.equal(requests.length, 0, "unchanged reopened editor should reuse all hydrated snapshots");
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), `${marker} original 0`, "cached metadata must be isolated from discarded draft");
    await close();

    const before = await get(page, `gallery/import-edit/${id}?light=1`);
    await json(await page.request.post(root + `gallery/import-edit/${id}`, { data: { revision: before.revision, items: before.items.map((item, index) => ({ image_id: item.image_id, overrides: index === 1 ? { prompt: `${marker} external changed` } : {} })) } }));
    const after = await get(page, `gallery/import-edit/${id}?light=1`);
    const changed = after.items.filter((item) => before.items.find((old) => old.image_id === item.image_id)?.item_revision !== item.item_revision).map((item) => item.image_id);
    requests.length = 0;
    await open(); for (let i = 0; i < 3; i++) await reveal(i);
    assert.deepEqual(requests.map((item) => item.imageId).sort(), changed.sort(), "only changed item revisions should be fetched after external editing");
    assert.equal(await cards.nth(1).locator('[data-import-field="prompt"]').inputValue(), `${marker} external changed`);
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), `${marker} original 0`);
    await close();

    // Close while a changed snapshot is in flight, then reopen the same revision.
    const current = await get(page, `gallery/import-edit/${id}?light=1`);
    holdImageId = current.items[0].image_id;
    await json(await page.request.post(root + `gallery/import-edit/${id}`, { data: { revision: current.revision, items: current.items.map((item, index) => ({ image_id: item.image_id, overrides: index === 0 ? { prompt: `${marker} pending latest` } : {} })) } }));
    requests.length = 0;
    await open(); await until(() => !!releaseRead, "changed snapshot request was not held"); await close();
    await open(); await pause(160);
    assert.equal(requests.filter((item) => item.imageId === holdImageId).length, 1, "reopen should join the same in-flight snapshot read");
    releaseRead();
    await reveal(0);
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), `${marker} pending latest`);
    assert.deepEqual(errors, []);
    await close();
    console.log(`${name} ${width}: preview reuse, immutable editor snapshot reuse, revision invalidation and in-flight reopen dedup passed`);
  } finally { releaseRead?.(); await page.unrouteAll({ behavior: "ignoreErrors" }); await page.close(); }
}

(async () => {
  for (const [name, engine, width] of [["chromium", chromium, 1440], ["webkit", webkit, 390]]) {
    const browser = await engine.launch({ headless: true });
    try { await verify(browser, name, width); } finally { await browser.close(); }
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
