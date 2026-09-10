/* Run only against a fresh tests/webui_harness.py instance, never a deployment. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const root = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio/`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-import-edit-"));
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]); marker=sys.argv[2];files=[]
for index in range(3):
 graph={
  "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"landscape-model.safetensors"}},
  "2":{"class_type":"TextInput","inputs":{"text":f"candidate landscape {marker} {index}"}},
  "3":{"class_type":"CustomTextTransform","inputs":{"text":["2",0]}},
  "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur"}},
  "5":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["3",0]}},
  "6":{"class_type":"EmptyLatentImage","inputs":{"width":320,"height":240,"batch_size":1}},
  "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":9007199254740993123,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "8":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}},
  "9":{"class_type":"SaveImage","inputs":{"images":["8",0],"filename_prefix":"landscape"}},
  "15":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":f"alternate meadow {marker} {index}"}},
  "17":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["15",0],"negative":["4",0],"latent_image":["6",0],"seed":9,"steps":16,"cfg":5,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "18":{"class_type":"VAEDecode","inputs":{"samples":["17",0],"vae":["1",2]}},
  "19":{"class_type":"SaveImage","inputs":{"images":["18",0],"filename_prefix":"meadow"}}
 }
 image=Image.new("RGB",(320,240),(170+index*10,204,218));draw=ImageDraw.Draw(image);draw.rectangle((0,155,320,240),fill=(105,150,135));draw.polygon([(0,160),(120,40),(250,160)],fill=(120,142,151))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",json.dumps(graph));metadata.add_text("BrowserFixture",f"{marker}-{index}")
 target=folder/f"{marker}-{index}.png";image.save(target,pnginfo=metadata);files.append(str(target))
print(json.dumps(files))
`;

async function waitUntil(check, message) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) { if (await check()) return; await new Promise((resolve) => setTimeout(resolve, 50)); }
  throw new Error(message);
}
async function get(page, endpoint) {
  const response = await page.request.get(root + endpoint); assert.ok(response.ok(), await response.text()); return response.json();
}
async function noticeOff(frame) { await frame.locator("#appNoticeClose").evaluate((node) => node.click()); }
async function chooseOutput(page, frame, card, value) {
  const select = card.locator("[data-import-output]");
  const index = await select.evaluate((node, target) => Array.from(node.options).findIndex((option) => option.value === target), value);
  const response = page.waitForResponse((item) => item.url().endsWith("/imports/inspect") && item.request().postDataJSON()?.output_node_id === value);
  await card.locator(".import-output-choice .studio-select-trigger").click();
  await frame.locator(`.studio-select-menu [data-option-index="${index}"]`).click();
  await response;
  await waitUntil(() => select.inputValue().then((selected) => selected === value), "branch did not settle");
}
async function pendingSnapshot(frame) {
  return frame.locator("#importGrid .import-card").evaluateAll((cards) => cards.map((card) => ({ id: card.dataset.importId, filename: card.querySelector("strong").textContent, values: Array.from(card.querySelectorAll("[data-import-field]")).map((field) => [field.dataset.importField, field.value]) })));
}
async function verify(browser, name, width) {
  const marker = `${path.basename(output)}-${name}-${width}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], edits = []; let uploads = 0, prepares = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.url().includes("/imports/upload/")) uploads++;
    if (request.url().endsWith("/imports/prepare")) prepares++;
    if (request.method() === "POST" && request.url().includes("/gallery/import-edit/")) edits.push(request.postDataJSON());
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator("body").evaluate(async (_, theme) => { await window.ImageStudioAppearance.ready; window.ImageStudioAppearance.set({ preference: theme }); }, width < 600 ? "dark" : "light");
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill("山间湖泊与日光"); await frame.locator("#gallerySearch").press("Enter");
    await frame.locator("#galleryRefresh").click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator("#detailDelete:not(:disabled)").waitFor();
    assert.equal(await frame.locator("#detailImportEdit").isVisible(), false, "generated records cannot be edited as imports");
    await frame.locator("#closeDrawer").click();
    await frame.locator('[data-view="import"]').click();
    await frame.locator("#importFiles").setInputFiles(files);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    const imports = frame.locator("#importGrid .import-card");
    await chooseOutput(page, frame, imports.first(), "9");
    await imports.first().locator("[data-batch-import-output]").click();
    await waitUntil(async () => (await imports.locator("[data-import-output]").evaluateAll((nodes) => nodes.map((node) => node.value))).every((value) => value === "9"), "bulk branch selection failed");
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    for (let index = 0; index < 3; index++) await imports.nth(index).locator('[data-import-field="prompt"]').fill(`${marker} original ${index}`);
    const idsBefore = await imports.evaluateAll((nodes) => nodes.map((node) => node.dataset.importId));
    await imports.first().locator("[data-sort-handle]").press("End");
    assert.deepEqual(await imports.evaluateAll((nodes) => nodes.map((node) => node.dataset.importId)), [idsBefore[1], idsBefore[2], idsBefore[0]]);
    assert.equal(await frame.locator("#importBatchSummary").isVisible(), false, "reorder invalidates batch result");
    await frame.locator("#importGroupOption .toggle-control").click();
    await frame.locator("#confirmImportButton").click();
    await waitUntil(() => imports.count().then((count) => count === 0), "import did not commit");
    const listing = await get(page, "gallery/list?source=import&query=" + encodeURIComponent(marker));
    assert.equal(listing.total, 1); const id = listing.items[0].id;
    let snapshot = await get(page, `gallery/import-edit/${id}`);
    assert.deepEqual(snapshot.items.map((item) => item.filename), [files[1], files[2], files[0]].map((file) => path.basename(file)));
    // Store an actual integer beyond JS precision and a sub-millisecond timestamp.
    const preservedTime = 1700000000.12567;
    const seedBody = { revision: snapshot.revision, items: snapshot.items.map((item, index) => ({ image_id: item.image_id, overrides: index === 0 ? { parameters: { seed: "EXACT_INTEGER" }, generated_at: preservedTime } : { generated_at: null } })) };
    const seeded = await page.request.post(root + `gallery/import-edit/${id}`, { headers: { "Content-Type": "application/json" }, data: JSON.stringify(seedBody).replace('"EXACT_INTEGER"', "9007199254740993123") });
    assert.ok(seeded.ok(), await seeded.text());
    snapshot = await get(page, `gallery/import-edit/${id}`);
    await frame.locator("#importFiles").setInputFiles(files[0]);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    await imports.first().locator('[data-import-field="prompt"]').fill("pending import draft must survive");
    const pending = await pendingSnapshot(frame), uploadCount = uploads, prepareCount = prepares;
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Enter");
    await frame.locator("#galleryRefresh").click();
    await frame.locator(`[data-gallery-id="${id}"] .gallery-info`).click();
    await frame.locator('[data-detail-dot="1"]').click();
    const viewedId = snapshot.items[1].image_id;
    const footer = await frame.locator("#detailFooter").evaluate((node) => {
      const box = node.getBoundingClientRect();
      return { width: box.width, scroll: node.scrollWidth, color: getComputedStyle(node.querySelector("#detailImportEdit")).color, hasIcon: !!node.querySelector("#detailImportEdit svg"), buttons: Array.from(node.querySelectorAll("button")).filter((button) => button.getClientRects().length).map((button) => ({ left: button.getBoundingClientRect().left, right: button.getBoundingClientRect().right })), left: box.left, right: box.right };
    });
    assert.ok(footer.hasIcon && footer.scroll <= footer.width + 1);
    assert.ok(footer.buttons.every((button) => button.left >= footer.left && button.right <= footer.right + 1));
    await frame.locator("#detailImportEdit").click();
    const grid = frame.locator("#importEditGrid"), cards = grid.locator(".import-card");
    await cards.nth(2).waitFor();
    assert.equal(await grid.locator("[data-remove-import]").count(), 0);
    assert.match(await cards.first().locator('[data-import-field="parameters"]').inputValue(), /9007199254740993123/);
    assert.equal(await cards.nth(1).locator('[data-import-field="generated_at"]').inputValue(), "");
    assert.equal(await frame.locator("body").evaluate((node) => getComputedStyle(node).overflow), "hidden");
    await cards.first().locator('[data-import-field="prompt"]').fill("discard this edit");
    await cards.first().locator("[data-sort-handle]").scrollIntoViewIfNeeded();
    const handleBox = await cards.first().locator("[data-sort-handle]").boundingBox();
    await page.mouse.move(handleBox.x + 15, handleBox.y + 15); await page.mouse.down();
    await page.mouse.move(handleBox.x + 30, handleBox.y + 32, { steps: 3 });
    await frame.locator(".is-sorting").waitFor();
    await page.keyboard.press("Escape"); await page.mouse.up();
    assert.equal(await grid.isVisible(), true, "Escape cancels the current drag without closing the editor");
    assert.equal(await frame.locator(".is-sorting").count(), 0);
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), "discard this edit");
    await cards.first().locator("[data-sort-handle]").press("End");
    await frame.locator(".studio-modal-scrim").click({ position: { x: 1, y: 1 }, force: true });
    assert.equal(await grid.isVisible(), true, "scrim does not discard editor");
    await frame.locator("#importEditCancel").click();
    assert.deepEqual(await pendingSnapshot(frame), pending);
    await frame.locator("#detailImportEdit:not(:disabled)").click();
    await cards.nth(2).waitFor();
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), `${marker} original 1`);
    assert.equal(await cards.first().getAttribute("data-import-id"), `edit_${snapshot.items[0].image_id}`);
    const updatedPrompt = `${marker} searchable edited landscape`;
    await cards.first().locator('[data-import-field="prompt"]').fill(updatedPrompt);
    await cards.first().locator(".import-prompt-candidates > summary").click();
    const candidate = cards.first().locator('[data-batch-candidate-target="prompt"]').first();
    await candidate.click();
    await frame.locator("#importEditSave:not(:disabled)").waitFor();
    assert.equal(await frame.locator("#importEditBatchSummary").isVisible(), true);
    for (let index = 0; index < 3; index++) assert.match(await cards.nth(index).locator('[data-import-field="prompt"]').inputValue(), /candidate landscape/);
    await cards.first().locator("[data-sort-handle]").press("End");
    const order = [snapshot.items[1].image_id, snapshot.items[2].image_id, snapshot.items[0].image_id];
    const geometry = await grid.evaluate((node) => ({ page: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth, grid: node.scrollWidth, client: node.clientWidth, handle: node.querySelector("[data-sort-handle]").getBoundingClientRect().height }));
    assert.ok(geometry.page <= geometry.viewport + 1 && geometry.grid <= geometry.client + 1, JSON.stringify(geometry));
    assert.ok(geometry.handle >= 40);
    await noticeOff(frame);
    await page.screenshot({ path: path.join(output, `${name}-${width}-editor.png`) });
    await frame.locator("#importEditSave").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator("#detailImportEdit:not(:disabled)").waitFor();
    assert.equal(await frame.locator('[data-detail-dot="0"]').getAttribute("aria-current"), "true", "viewed image follows its identity after reorder");
    let saved = await get(page, `gallery/import-edit/${id}`);
    assert.deepEqual(saved.items.map((item) => item.image_id), order);
    assert.equal(saved.items[0].image_id, viewedId);
    assert.equal(saved.items[2].fields.generated_at, preservedTime);
    assert.match(saved.items[2].parameters_json, /9007199254740993123/);
    const body = edits.at(-1);
    assert.deepEqual(body.items.map((item) => item.image_id), order);
    assert.ok(body.items.every((item) => !Object.hasOwn(item.overrides, "parameters") && !Object.hasOwn(item.overrides, "generated_at")));
    assert.equal((await get(page, "gallery/list?query=" + encodeURIComponent(updatedPrompt))).total, 1);
    assert.equal(uploads, uploadCount); assert.equal(prepares, prepareCount);
    assert.deepEqual(await pendingSnapshot(frame), pending);
    await frame.locator("#detailImportEdit").click();
    await cards.nth(2).waitFor();
    assert.deepEqual(await cards.evaluateAll((nodes) => nodes.map((node) => node.dataset.importId)), order.map((imageId) => `edit_${imageId}`));
    assert.match(await cards.nth(2).locator('[data-import-field="prompt"]').inputValue(), /searchable edited landscape/);
    let releaseInspect;
    await page.route("**/imports/inspect", async (route) => {
      if (route.request().postDataJSON()?.output_node_id === "19" && !releaseInspect) await new Promise((resolve) => { releaseInspect = resolve; });
      await route.continue();
    });
    const inspecting = chooseOutput(page, frame, cards.first(), "19");
    await waitUntil(() => Promise.resolve(!!releaseInspect), "branch inspection was not delayed");
    assert.equal(await frame.locator("#importEditSave").isDisabled(), true);
    assert.ok((await grid.locator("[data-sort-handle]").evaluateAll((nodes) => nodes.map((node) => node.disabled))).every(Boolean));
    const concurrentPrompt = `${marker} retained while another branch is reading`;
    await cards.nth(1).locator('[data-import-field="prompt"]').fill(concurrentPrompt);
    releaseInspect(); await inspecting;
    assert.equal(await cards.nth(1).locator('[data-import-field="prompt"]').inputValue(), concurrentPrompt);
    await cards.first().locator("[data-batch-import-output]").click();
    await waitUntil(async () => (await cards.locator("[data-import-output]").evaluateAll((nodes) => nodes.map((node) => node.value))).every((value) => value === "19"), "editor batch output selection failed");
    await frame.locator("#importEditSave:not(:disabled)").waitFor();
    assert.equal(await cards.nth(1).locator('[data-import-field="prompt"]').inputValue(), concurrentPrompt);
    await frame.locator("#importEditSave").click(); await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    saved = await get(page, `gallery/import-edit/${id}`);
    assert.ok(saved.items.every((item) => item.output_node_id === "19"));
    assert.equal(saved.items[1].fields.prompt, concurrentPrompt);
    assert.match(saved.items[2].parameters_json, /9007199254740993123/);
    assert.equal(uploads, uploadCount); assert.equal(prepares, prepareCount);
    assert.deepEqual(await pendingSnapshot(frame), pending);
    await page.unroute("**/imports/inspect");
    await frame.locator("#detailImportEdit:not(:disabled)").click(); await cards.nth(2).waitFor();
    // A stale save must keep the draft visible with the backend's conflict message.
    const external = await page.request.post(root + `gallery/import-edit/${id}`, { data: { revision: saved.revision, items: saved.items.map((item, index) => ({ image_id: item.image_id, overrides: index ? {} : { negative_prompt: "external update" } })) } });
    assert.ok(external.ok(), await external.text());
    await cards.first().locator('[data-import-field="prompt"]').fill("stale local draft");
    await frame.locator("#importEditSave").click();
    await waitUntil(() => frame.locator("#studioModalError").textContent().then((value) => !!value), "missing conflict message");
    assert.equal(await grid.isVisible(), true);
    assert.equal(await cards.first().locator('[data-import-field="prompt"]').inputValue(), "stale local draft");
    await frame.locator("#importEditCancel").click();
    await frame.locator("#closeDrawer").click();
    await frame.locator('[data-view="import"]').click();
    assert.deepEqual(await pendingSnapshot(frame), pending);
    await frame.locator("#cancelImportButton").click();
    const singles = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, `${marker}-single`], { encoding: "utf8" }));
    await frame.locator("#importFiles").setInputFiles(singles[0]);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    await chooseOutput(page, frame, imports.first(), "9");
    await imports.first().locator('[data-import-field="prompt"]').fill(`${marker}-single`);
    await frame.locator("#confirmImportButton").click();
    await waitUntil(() => imports.count().then((count) => count === 0), "single import did not commit");
    const single = await get(page, "gallery/list?query=" + encodeURIComponent(`${marker}-single`));
    assert.equal(single.total, 1); assert.equal(single.items[0].image_count, 1);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(`${marker}-single`); await frame.locator("#gallerySearch").press("Enter"); await frame.locator("#galleryRefresh").click();
    await frame.locator(`[data-gallery-id="${single.items[0].id}"] .gallery-info`).click();
    await frame.locator("#detailImportEdit").click(); await cards.first().waitFor();
    assert.equal(await cards.count(), 1); assert.equal(await cards.first().locator("[data-sort-handle]").isDisabled(), true);
    await cards.first().locator('[data-import-field="model"]').fill("edited single model");
    await frame.locator("#importEditSave").click(); await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal((await get(page, `gallery/import-edit/${single.items[0].id}`)).items[0].fields.model, "edited single model");
    assert.deepEqual(errors, []);
    console.log(`${name} ${width}: import reorder, edit/cancel/reopen, drag Escape, async and batch branches, exact metadata, stale save, single record, themed layout and isolated draft passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await engine.launch({ headless: true });
    try { for (const width of [1440, 390]) await verify(browser, name, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
