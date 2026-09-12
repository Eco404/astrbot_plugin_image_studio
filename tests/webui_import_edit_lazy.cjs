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
async function seed(page, marker) {
  const encoded = JSON.parse(execFileSync(python, ["-c", String.raw`
import base64,io,json,sys
from PIL import Image,PngImagePlugin
out=[]
for i in range(18):
 graph={"1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"lazy-model"}},"2":{"class_type":"TextInput","inputs":{"text":f"own candidate {i} {sys.argv[1]}"}},"3":{"class_type":"CustomTextTransform","inputs":{"text":["2",0]}},"4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur"}},"5":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["3",0]}},"6":{"class_type":"EmptyLatentImage","inputs":{"width":96,"height":64,"batch_size":1}},"7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":i,"steps":20,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},"8":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}},"9":{"class_type":"SaveImage","inputs":{"images":["8",0],"filename_prefix":"lazy"}}}
 info=PngImagePlugin.PngInfo(); info.add_text("prompt",json.dumps(graph)); info.add_text("FixtureNotes",sys.argv[1]+"x"*120000)
 image=Image.new("RGB",(96,64),(i*10,120,180)); buffer=io.BytesIO(); image.save(buffer,"PNG",pnginfo=info); out.append(base64.b64encode(buffer.getvalue()).decode())
print(json.dumps(out))
`, marker], { encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }));
  const buffers = encoded.map((item) => Buffer.from(item, "base64"));
  const items = buffers.map((buffer, index) => ({ client_id: `lazy_${index}`, sha256: crypto.createHash("sha256").update(buffer).digest("hex"), filename: `lazy-${index}.png`, overrides: { prompt: `${marker} original ${index}`, model: "lazy-model" } }));
  const prepared = await json(await page.request.post(root + "imports/prepare", { data: { as_group: true, items } }));
  for (let index = 0; index < buffers.length; index++) await json(await page.request.post(root + prepared.items[index].upload_endpoint, { multipart: { file: { name: items[index].filename, mimeType: "image/png", buffer: buffers[index] } } }));
  await json(await page.request.post(root + prepared.commit_endpoint, { data: {} }));
  const listing = await json(await page.request.get(root + "gallery/list?query=" + encodeURIComponent(marker)));
  assert.equal(listing.total, 1);
  return listing.items[0].id;
}

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 } });
  page.setDefaultTimeout(20000);
  const marker = `lazy-editor-${name}-${width}-${Date.now()}`;
  const id = await seed(page, marker);
  const edits = [], itemRequests = [], errors = [];
  let mode = "hold-manifest", heldManifest, heldItems = [], active = 0, maxActive = 0, completed = 0, fullGroups = 0, uploadCount = 0, failedImageId;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.url().includes("/imports/upload/") || request.url().endsWith("/imports/prepare")) uploadCount++;
    if (request.method() === "POST" && request.url().includes("/gallery/import-edit/")) edits.push(request.postDataJSON());
  });
  await page.route(`**/gallery/import-edit/${id}*`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const url = new URL(route.request().url());
    if (url.searchParams.get("light") === "1") {
      if (mode === "hold-manifest") await new Promise((resolve) => { heldManifest = resolve; });
      if (mode === "fail-manifest") { mode = "hold-items"; return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "fixture list unavailable" }) }); }
      return route.continue();
    }
    if (!url.searchParams.has("image_id")) { fullGroups++; return route.continue(); }
    itemRequests.push(url.searchParams.get("image_id")); active++; maxActive = Math.max(maxActive, active);
    try {
      if (mode === "fail-item") {
        mode = "normal"; failedImageId = url.searchParams.get("image_id");
        return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "fixture item unavailable" }) });
      }
      if (mode === "hold-items") await new Promise((resolve) => heldItems.push(resolve));
      else await pause(120);
      const response = await route.fetch(); await route.fulfill({ response });
    } finally { active--; completed++; }
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Enter");
    await frame.locator(`[data-gallery-id="${id}"] .gallery-info`).click();
    await frame.locator("#detailImportEdit:not(:disabled)").click();
    await frame.locator("#importEditLoading").waitFor({ state: "visible" });
    await until(() => !!heldManifest, "manifest was not held");
    assert.equal(await frame.locator("#importEditSave").isDisabled(), true);
    assert.equal(itemRequests.length, 0);
    await frame.locator("#importEditCancel").click(); heldManifest();
    await pause(200);
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), false, "late manifest must not reopen editor");
    assert.equal(itemRequests.length, 0);

    mode = "fail-manifest";
    await frame.locator("#detailImportEdit:not(:disabled)").click();
    await frame.locator("#importEditRetry").waitFor({ state: "visible" });
    await frame.locator("#importEditRetry").click();
    const grid = frame.locator("#importEditGrid"), cards = grid.locator(".import-card");
    await until(() => cards.count().then((count) => count === 18), "manifest must retain every sortable ID");
    await until(() => heldItems.length > 0, "visible cards not requested");
    await pause(160);
    assert.ok(itemRequests.length <= 3, `initial visible hydration requested ${itemRequests.length}`);
    assert.equal(await grid.locator("[data-import-field]").count(), 0);
    if (width > 600) {
      const handle = cards.first().locator("[data-sort-handle]");
      await handle.evaluate((node) => { window.__lazyDragHandle = node; });
      const box = await handle.boundingBox();
      await page.mouse.move(box.x + 15, box.y + 15); await page.mouse.down();
      await page.mouse.move(box.x + 28, box.y + 28, { steps: 3 });
      await frame.locator("#importEditGrid.is-sorting").waitFor();
      heldItems.shift()();
      await until(() => completed > 0, "dragged item's hydration did not finish");
      await pause(80);
      assert.equal(await frame.locator("#importEditGrid.is-sorting").count(), 1, "hydration must not cancel pointer capture");
      assert.equal(await handle.evaluate((node) => node === window.__lazyDragHandle), true, "captured handle must survive hydration");
      await page.mouse.up();
      await cards.first().locator('[data-import-field="prompt"]').waitFor({ state: "attached" });
    }
    const originalOrder = await cards.evaluateAll((nodes) => nodes.map((node) => node.dataset.importId.slice(5)));
    await cards.last().locator("[data-sort-handle]").press("Home");
    const reordered = [originalOrder.at(-1), ...originalOrder.slice(0, -1)];
    assert.deepEqual(await cards.evaluateAll((nodes) => nodes.map((node) => node.dataset.importId.slice(5))), reordered);
    assert.equal(await frame.locator("#importEditSave").isDisabled(), false, "untouched pending lazy reads do not block reorder save");
    await frame.locator("#importEditSave").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.deepEqual(edits.at(-1).items, reordered.map((image_id) => ({ image_id, overrides: {} })));
    const requestsAtClose = itemRequests.length;
    mode = "normal"; heldItems.splice(0).forEach((resolve) => resolve());
    await until(() => active === 0, "held card requests did not finish");
    assert.equal(itemRequests.length, requestsAtClose, "closing must discard queued hydration");
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), false);

    itemRequests.length = 0; mode = "fail-item";
    await frame.locator("#detailImportEdit:not(:disabled)").click();
    await until(() => !!failedImageId, "item failure was not exercised");
    const failedCard = grid.locator(`[data-import-id="edit_${failedImageId}"]`);
    await failedCard.locator(".import-card-status.is-error").waitFor();
    await failedCard.locator("[data-import-load]").click();
    await cards.first().locator('[data-import-field="prompt"]').waitFor({ state: "attached" });
    assert.equal(await cards.first().locator(".prompt-candidate").count(), 0, "candidate DOM is deferred");
    assert.equal(await cards.first().locator('[data-import-field="parameters"]').count(), 0, "parameter JSON textarea is deferred");
    assert.equal(await cards.first().locator(".comfy-stage").count(), 0, "workflow stages are deferred");
    assert.ok(itemRequests.length < 18, "opening must not fetch every editable item");
    const changedPrompt = `${marker} keep my draft`;
    await cards.first().locator('[data-import-field="prompt"]').fill(changedPrompt);
    await cards.first().evaluate((node) => { window.__lazyEditorDraftNode = node; window.__lazyEditorPromptNode = node.querySelector('[data-import-field="prompt"]'); });
    await cards.nth(7).evaluate((node) => node.scrollIntoView({ block: "start" }));
    await cards.nth(7).locator('[data-import-field="prompt"]').waitFor({ state: "attached" });
    assert.equal(await cards.first().evaluate((node) => node === window.__lazyEditorDraftNode && node.querySelector('[data-import-field="prompt"]') === window.__lazyEditorPromptNode), true, "unrelated hydration must preserve editable DOM");
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), changedPrompt);
    await cards.first().locator(".import-prompt-candidates > summary").click();
    await cards.first().locator('[data-batch-candidate-target="prompt"]').first().click();
    await frame.locator("#importEditSave:disabled").waitFor();
    await frame.locator("#importEditSave:not(:disabled)").waitFor();
    assert.equal(new Set(itemRequests).size, 18, "explicit batch deliberately hydrates all frozen targets");
    assert.ok(maxActive <= 3, `hydration concurrency exceeded 3: ${maxActive}`);
    assert.ok(await grid.locator(".import-card-placeholder").count() > 0, "batch hydration must not materialize every offscreen editor");
    await frame.locator("#importEditSave").click(); await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    const saved = await json(await page.request.get(root + `gallery/import-edit/${id}`));
    for (const item of saved.items) {
      const index = Number(item.filename.match(/lazy-(\d+)/)[1]);
      assert.ok(item.fields.prompt.includes(`own candidate ${index} ${marker}`), "batch applies each image's own text");
    }
    assert.ok(saved.items[0].fields.prompt.includes(changedPrompt));
    assert.equal(fullGroups, 0); assert.equal(uploadCount, 0); assert.deepEqual(errors, []);
    console.log(`${name} ${width}: immediate shell, manifest retry/cancel, 18 IDs, max3 lazy hydration, unloaded reorder save, deferred sections, DOM continuity and explicit batch passed`);
  } finally { heldManifest?.(); heldItems.splice(0).forEach((resolve) => resolve()); await page.unrouteAll({ behavior: "ignoreErrors" }); await page.close(); }
}

(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await engine.launch({ headless: true });
    try { for (const width of [1440, 390]) await verify(browser, name, width); }
    finally { await browser.close(); }
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
