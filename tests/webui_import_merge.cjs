/* Existing-group imports against an isolated harness, with safe generated PNGs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-import-merge-"));
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const runId = Date.now().toString(36);

const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]); paths=[]
for i in range(6):
 image=Image.new("RGB",(320,240),(175+i*3,204,210));draw=ImageDraw.Draw(image);draw.rectangle((0,150,320,240),fill=(103,148+i*3,138));draw.polygon([(0,165),(125,48),(250,165)],fill=(116,140,151));draw.ellipse((226,32,277,61),fill=(232,239,230))
 params={"prompt":f"safe landscape merge image {i}","uc":"blur, watermark","model":f"merge-model-{i}","steps":20+i,"scale":6,"cfg_rescale":0.3,"seed":10+i,"width":320,"height":240,"request_type":"PromptGenerateRequest"}
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps(params));metadata.add_text("BrowserFixture",sys.argv[2] if len(sys.argv)>2 else folder.name);file=folder/f"merge-{i}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;
let fixtures = JSON.parse(execFileSync(python, ["-c", fixtureScript, output], { encoding: "utf8" }));

async function body(response) { const payload = await response.json(); return payload.data || payload; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${method} ${endpoint}: ${response.status()} ${await response.text()}`);
  return await body(response);
}

async function importSeed(page, name, index = 0) {
  const bytes = execFileSync(python, ["-c", "import sys,io; from PIL import Image,PngImagePlugin; image=Image.open(sys.argv[1]); metadata=PngImagePlugin.PngInfo(); [metadata.add_text(k,v) for k,v in image.info.items() if isinstance(v,str)]; metadata.add_text('MergeFixture',sys.argv[2]); output=io.BytesIO(); image.save(output,format='PNG',pnginfo=metadata); sys.stdout.buffer.write(output.getvalue())", fixtures[index], name]);
  const prepared = await api(page, "post", "imports/prepare", { items: [{ client_id: `${runId}_${name}`, filename: `${name}.png`, sha256: createHash("sha256").update(bytes).digest("hex"), overrides: { generation_engine: "novelai", model: name, prompt: `safe seeded landscape ${name}` } }] });
  const response = await page.request.post(`${apiRoot}/${prepared.items[0].upload_endpoint}`, { multipart: { file: { name: `${name}.png`, mimeType: "image/png", buffer: bytes } } });
  assert.ok(response.ok());
  return (await api(page, "post", prepared.commit_endpoint, {})).generation_id;
}

async function settle(inner) {
  await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))));
}

async function capture(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await settle(inner);
  const shape = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(shape.scroll <= shape.width + 1, `${name}: page overflow ${JSON.stringify(shape)}`);
  if (await inner.locator("#studioModalRoot").isVisible()) {
    const modal = await inner.locator("#studioModal").boundingBox(); assert.ok(modal.x >= 0 && modal.x + modal.width <= shape.width + 1);
    const footer = await inner.locator("#studioModalFooter").boundingBox(); assert.ok(footer.y + footer.height <= page.viewportSize().height + 1);
  }
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function toggle(frame, id, checked) {
  if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
  if (await frame.locator(`#${id}`).isChecked() !== checked) await frame.locator(`#${id}`).locator("xpath=..").click();
  assert.equal(await frame.locator(`#${id}`).isChecked(), checked);
}

async function chooseCardEngine(page, card, value) {
  const source = card.locator('[data-import-field="generation_engine"]');
  const index = await source.evaluate((select, requested) => Array.from(select.options).findIndex((option) => option.value === requested), value);
  assert.ok(index >= 0); await source.locator("xpath=..").locator(".studio-select-trigger").click();
  await page.frameLocator("#studio").locator(`.studio-select-menu [data-option-index="${index}"]`).click();
}

async function stage(frame, files) {
  await frame.locator("#importFiles").setInputFiles(files);
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  assert.equal(await frame.locator(".import-card").count(), files.length);
}

async function openPicker(frame) {
  await frame.locator("#confirmImportButton").click();
  await frame.locator("#importMergeTargets").waitFor();
}

async function target(frame, id) {
  if (!await frame.locator(`[data-merge-target="${id}"]`).count()) {
    await frame.locator("#importMergePrev:not(:disabled)").click(); await frame.locator("#importMergePage").filter({ hasText: "第 1" }).waitFor();
  }
  await frame.locator(`[data-merge-target="${id}"]`).click();
  assert.equal(await frame.locator('input[name="importMergeTarget"]:checked').count(), 1);
  assert.equal(await frame.locator('input[name="importMergeTarget"]:checked').inputValue(), id);
}

async function cleared(frame) { await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" }); }

async function verifyBridgeContract(inner, network) {
  const before = network.targets;
  const rejected = await inner.evaluate(async () => {
    const paths = ["imports/merge-targets?generation_engine=novelai", "imports/merge-targets#fragment", "imports/../merge-targets"];
    return await Promise.all(paths.map(async (path) => {
      try { await window.AstrBotPluginPage.apiGet(path); return "accepted"; }
      catch (error) { return error.message; }
    }));
  });
  assert.ok(rejected.every(message => message === "Plugin bridge endpoint is invalid."));
  assert.equal(network.targets, before, "invalid endpoints must fail before any backend request");
  const result = await inner.evaluate(() => window.AstrBotPluginPage.apiGet("imports/merge-targets", { generation_engine: "novelai", limit: 1, offset: 0 }));
  assert.equal(result.items.length, 1);
  assert.equal(result.limit, 1);
  assert.equal(result.offset, 0);
  assert.equal(network.targets, before + 1);
}

async function rejectCases(page, frame, inner, name, network) {
  await stage(frame, fixtures.slice(1, 3));
  await toggle(frame, "importAsGroup", true); await toggle(frame, "importMergeExisting", true);
  assert.equal(await frame.locator("#importAsGroup").isChecked(), false);
  await toggle(frame, "importAsGroup", true); assert.equal(await frame.locator("#importMergeExisting").isChecked(), false);
  await toggle(frame, "importMergeExisting", true);
  await chooseCardEngine(page, frame.locator(".import-card").nth(1), "gemini");
  const before = { ...network };
  await frame.locator("#confirmImportButton").click(); await frame.locator("#appNoticeMessage").filter({ hasText: "生图来源必须相同" }).waitFor();
  assert.equal(network.prepare, before.prepare); assert.equal(network.upload, before.upload); assert.equal(network.targets, before.targets);
  await frame.locator("#cancelImportButton").click();
  await stage(frame, [fixtures[4]]); await chooseCardEngine(page, frame.locator(".import-card").first(), "gemini"); await toggle(frame, "importMergeExisting", true);
  await frame.locator("#confirmImportButton").click(); await frame.locator("#appNoticeMessage").filter({ hasText: "暂无同源的已导入图组" }).waitFor();
  assert.equal(await frame.locator("#importMergeTargets").count(), 0); assert.equal(await frame.locator(".import-card").count(), 1);
  assert.equal(network.prepare, before.prepare); assert.equal(network.upload, before.upload);
  await capture(page, inner, `${name}-no-compatible-group`); await frame.locator("#cancelImportButton").click();
}

async function paginationAndRetry(page, frame, inner, name, targetId, network) {
  await stage(frame, fixtures.slice(1, 3)); await toggle(frame, "importMergeExisting", true);
  await frame.locator('.import-card [data-import-field="model"]').nth(0).fill(`${name}-model-B`);
  await frame.locator('.import-card [data-import-field="model"]').nth(1).fill(`${name}-model-C`);
  const beforeTotal = (await api(page, "get", "gallery/list?limit=1")).total;
  const maskFull = async (route) => {
    const response = await route.fetch(); const original = await response.json(); const result = original.data || original;
    const other = result.items.find((item) => item.id !== targetId); if (other) other.image_count = 100;
    await route.fulfill({ response, json: original });
  };
  await page.route("**/imports/merge-targets?*", maskFull);
  await openPicker(frame);
  assert.equal(await frame.locator("[data-merge-target]").count(), 12);
  assert.ok(await frame.locator('input[name="importMergeTarget"]:disabled').count());
  await target(frame, targetId); await capture(page, inner, `${name}-choose-existing`);
  await frame.locator("#importMergeNext").click(); await frame.locator("#importMergePage").filter({ hasText: "第 2" }).waitFor();
  const nextChoice = frame.locator('[data-merge-target]:has(input:not(:disabled))').first(); const nextId = await nextChoice.getAttribute("data-merge-target");
  await nextChoice.click(); assert.equal(await frame.locator('input[name="importMergeTarget"]:checked').count(), 1);
  await frame.locator("#importMergePrev").click(); await frame.locator("#importMergePage").filter({ hasText: "第 1" }).waitFor();
  assert.equal(await frame.locator('input[name="importMergeTarget"]:checked').count(), 0, "old first-page selection must not remain after choosing second page");
  assert.ok(nextId !== targetId);
  await frame.locator("#studioModalClose").click(); await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  assert.equal(await frame.locator(".import-card").count(), 2); await page.unroute("**/imports/merge-targets?*", maskFull);

  let attempt = 0; let firstPath = ""; const successful = new Map();
  const failSecond = async (route) => {
    const url = route.request().url(); attempt++;
    if (attempt === 2) { await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试第二张暂时上传失败" }) }); return; }
    if (!firstPath) firstPath = url; successful.set(url, (successful.get(url) || 0) + 1); await route.continue();
  };
  await page.route("**/imports/upload/*", failSecond);
  const prepareBefore = network.prepare;
  await openPicker(frame); await target(frame, targetId); await frame.locator("#importMergeConfirm").click();
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); await frame.locator("#importProgress").filter({ hasText: "失败" }).waitFor();
  assert.equal(attempt, 2); assert.equal(await frame.locator(".import-card").count(), 2);
  const beforeFailure = await api(page, "get", `gallery/detail/${targetId}?assets=0`); assert.equal(beforeFailure.images.length, 1, "failed staged uploads must not partially merge");
  await frame.locator("#confirmImportButton").click(); await cleared(frame);
  assert.equal(attempt, 3); assert.equal(successful.get(firstPath), 1, "retry reuploaded an already successful image"); assert.equal(network.prepare, prepareBefore + 1, "retry should reuse the existing draft");
  await page.unroute("**/imports/upload/*", failSecond);
  const merged = await api(page, "get", `gallery/detail/${targetId}?assets=0`);
  assert.equal(merged.images.length, 3); assert.equal((await api(page, "get", "gallery/list?limit=1")).total, beforeTotal, "merge created a new generation record");
  assert.deepEqual(merged.images.slice(1).map((image) => image.supplemental.model || image.supplemental.overrides?.model), [`${name}-model-B`, `${name}-model-C`]);
  assert.deepEqual(merged.images.slice(1).map((image) => image.metadata.normalized.steps), [21, 22]);
}

async function lostCommitResponse(page, frame, name, targetId, network) {
  await stage(frame, [fixtures[3]]); assert.equal(await frame.locator("#importMergeOption").isVisible(), true); await toggle(frame, "importMergeExisting", true);
  let failed = false;
  const responseLost = async (route) => {
    if (!failed) { failed = true; const completed = await route.fetch(); assert.ok(completed.ok()); await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试提交响应丢失，请重试" }) }); }
    else await route.continue();
  };
  await page.route("**/imports/group/*/commit", responseLost);
  await openPicker(frame); await target(frame, targetId); await frame.locator("#importMergeConfirm").click();
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); await frame.locator("#importProgress").filter({ hasText: "重试" }).waitFor();
  assert.equal((await api(page, "get", `gallery/detail/${targetId}?assets=0`)).images.length, 4);
  const before = { ...network }; await frame.locator("#confirmImportButton").click(); await cleared(frame);
  assert.equal(network.upload, before.upload); assert.equal(network.prepare, before.prepare); assert.equal(network.targets, before.targets);
  assert.equal((await api(page, "get", `gallery/detail/${targetId}?assets=0`)).images.length, 4, "lost-response retry duplicated a committed image");
  await page.unroute("**/imports/group/*/commit", responseLost);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const seed = await browser.newPage(); await seed.goto(base);
    assert.equal(await seed.locator("#studio").count(), 1, "expected the isolated test harness");
    for (let index = 0; index < 14; index++) await importSeed(seed, `merge-target-${index}-${runId}`, index % 6);
    await seed.close();
    for (const test of [{ width: 1440, theme: "light" }, { width: 1100, theme: "dark" }, { width: 390, theme: "light" }, { width: 320, theme: "dark" }]) {
      const name = `${test.width}-${test.theme}`; const page = await browser.newPage({ viewport: { width: test.width, height: test.width < 600 ? 844 : 1000 }, hasTouch: test.width < 600 });
      fixtures = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, `${name}-${runId}`], { encoding: "utf8" }));
      page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const targetId = await importSeed(page, `${name}-target-A-${runId}`);
      const inner = page.frames().find((item) => item.url().includes("/ui/")); await inner.evaluate((value) => { document.documentElement.dataset.theme = value; }, test.theme);
      const network = { prepare: 0, upload: 0, targets: 0 };
      page.on("request", (request) => { if (request.url().includes("/imports/prepare")) network.prepare++; if (request.url().includes("/imports/upload/")) network.upload++; if (request.url().includes("/imports/merge-targets")) network.targets++; });
      await verifyBridgeContract(inner, network);
      await frame.locator('[data-view="import"]').click();
      await rejectCases(page, frame, inner, name, network);
      await paginationAndRetry(page, frame, inner, name, targetId, network);
      await lostCommitResponse(page, frame, name, targetId, network);
      await inner.evaluate(() => window.scrollTo(0, 0)); await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#gallerySearch").fill(`${name}-target-A-${runId}`); await frame.locator("#gallerySearch").press("Tab");
      await frame.locator(`[data-gallery-id="${targetId}"]`).waitFor(); await frame.locator(`[data-gallery-id="${targetId}"] .gallery-info`).click();
      await frame.locator('.detail-carousel-dot[data-detail-dot="1"]').click();
      await frame.locator("#drawerBody").filter({ hasText: `${name}-model-B` }).waitFor(); await capture(page, inner, `${name}-merged-detail`);
      assert.deepEqual(errors, []); await page.close(); console.log(`${name}: merge validation, pagination, cancellation, two upload retries, single-image append, per-image parameters passed`);
    }
    console.log(`Merge screenshots and safe fixtures: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
