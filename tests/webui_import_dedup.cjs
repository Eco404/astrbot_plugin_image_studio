/* Content-addressed import regression tests. Only use an isolated harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated import-dedup harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-import-dedup-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
let serial = 0;
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");

function fixture(name) {
  const marker = `${path.basename(output)}-${name}-${++serial}`;
  const bytes = execFileSync(python, ["-c", "import sys,io,json; from PIL import Image,ImageDraw,PngImagePlugin; image=Image.new('RGB',(320,240),(174,205,211)); draw=ImageDraw.Draw(image); draw.rectangle((0,150,320,240),fill=(103,148,138)); draw.polygon([(0,165),(125,48),(250,165)],fill=(116,140,151)); draw.ellipse((226,32,277,61),fill=(232,239,230)); metadata=PngImagePlugin.PngInfo(); metadata.add_text('Software','NovelAI'); metadata.add_text('Comment',json.dumps({'prompt':'safe landscape '+sys.argv[1],'uc':'blur','model':'dedup-test-model','steps':24,'seed':42,'request_type':'PromptGenerateRequest'})); stream=io.BytesIO(); image.save(stream,format='PNG',pnginfo=metadata); sys.stdout.buffer.write(stream.getvalue())", marker]);
  return { name: `${marker}.png`, mimeType: "image/png", buffer: bytes, sha256: hash(bytes) };
}

function uploadFile(value, name = value.name) { return { name, mimeType: value.mimeType, buffer: value.buffer }; }
async function decoded(response) { const value = await response.json(); return value.data || value; }
async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  return await decoded(response);
}

async function seed(page, image) {
  const clientId = `seed_${Date.now().toString(36)}_${++serial}`;
  const prepared = await api(page, "post", "imports/prepare", { items: [{ client_id: clientId, filename: image.name, sha256: image.sha256, overrides: {} }] });
  assert.equal(prepared.allowed, true);
  const response = await page.request.post(`${apiRoot}/${prepared.items[0].upload_endpoint}`, { multipart: { file: uploadFile(image) } });
  assert.ok(response.ok()); const upload = await decoded(response); assert.notEqual(upload.allowed, false);
  const result = await api(page, "post", prepared.commit_endpoint, {}); assert.equal(result.allowed, true);
  return result.generation_ids[0];
}

async function stage(frame, images) {
  await frame.locator("#importFiles").setInputFiles(images.map((image) => uploadFile(image)));
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
}

async function clear(frame) {
  if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
  await frame.locator("#cancelImportButton").click();
  await frame.locator(".import-card").waitFor({ state: "detached" });
}

async function toggle(frame, id, value) {
  if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
  if (await frame.locator(`#${id}`).isChecked() !== value) await frame.locator(`#${id}`).locator("..").click();
}

async function capture(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))));
  const shape = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(shape.scroll <= shape.width + 1, `${name}: horizontal overflow ${JSON.stringify(shape)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function importComplete(page, frame, mode, targetId = "") {
  const committed = page.waitForResponse((response) => /\/imports\/group\/[^/]+\/commit/.test(response.url()));
  await frame.locator("#confirmImportButton").click();
  if (mode === "merge") {
    await frame.locator(`[data-merge-target="${targetId}"]`).click(); await frame.locator("#importMergeConfirm").click();
  }
  const result = await decoded(await committed);
  await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" });
  return result;
}

async function localDedup(page, frame, inner, name) {
  const image = fixture(`${name}-local`);
  await frame.locator("#importFiles").setInputFiles([uploadFile(image), uploadFile(image, "same-bytes-other-name.png")]);
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); assert.equal(await frame.locator(".import-card").count(), 1);
  assert.equal(await frame.locator(".import-card").getAttribute("data-import-sha256"), image.sha256);
  await frame.locator("#importFiles").setInputFiles(uploadFile(image, "selected-again.png"));
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); assert.equal(await frame.locator(".import-card").count(), 1);
  await clear(frame);
  const fallback = await inner.evaluate(async (bytes) => {
    const original = Object.getOwnPropertyDescriptor(window.crypto, "subtle");
    Object.defineProperty(window.crypto, "subtle", { value: undefined, configurable: true });
    try { return await window.ImageStudioHash.fileSHA256(new File([Uint8Array.from(bytes)], "fallback.png", { type: "image/png" })); }
    finally { if (original) Object.defineProperty(window.crypto, "subtle", original); else delete window.crypto.subtle; }
  }, Array.from(image.buffer));
  assert.equal(fallback, image.sha256, "non-secure-context SHA fallback must match Node's SHA-256");

  await inner.evaluate(() => {
    const original = window.ImageStudioHash.fileSHA256;
    window.__originalHash = original;
    window.__hashGate = new Promise((resolve) => { window.__releaseHash = resolve; });
    window.ImageStudioHash.fileSHA256 = async (file) => { await window.__hashGate; return original(file); };
  });
  await frame.locator("#importFiles").setInputFiles(uploadFile(image));
  assert.equal(await frame.locator("#confirmImportButton").isDisabled(), true, "hashing must block confirm");
  await inner.evaluate((bytes) => {
    const input = document.getElementById("importFiles");
    for (const name of ["parallel-a.png", "parallel-b.png"]) { const transfer = new DataTransfer(); transfer.items.add(new File([Uint8Array.from(bytes)], name, { type: "image/png" })); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true })); }
    window.__releaseHash();
  }, Array.from(image.buffer));
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); assert.equal(await frame.locator(".import-card").count(), 1, "concurrent file additions must deduplicate");
  await inner.evaluate(() => { window.ImageStudioHash.fileSHA256 = window.__originalHash; }); await clear(frame);

  await inner.evaluate(() => { window.__hashGate = new Promise((resolve) => { window.__releaseHash = resolve; }); window.ImageStudioHash.fileSHA256 = async (file) => { await window.__hashGate; return window.__originalHash(file); }; });
  await frame.locator("#importFiles").setInputFiles(uploadFile(image));
  await frame.locator("#cancelImportButton").click();
  await inner.evaluate(() => { window.__releaseHash(); window.ImageStudioHash.fileSHA256 = window.__originalHash; });
  await page.waitForTimeout(120); assert.equal(await frame.locator(".import-card").count(), 0, "cancelled hash work must not recreate cards");
}

async function duplicateBatch(page, frame, inner, name, mode, traffic) {
  const existing = fixture(`${name}-${mode}-existing`); const fresh = fixture(`${name}-${mode}-new`); const targetId = await seed(page, existing);
  const existingImageId = (await api(page, "get", `gallery/detail/${targetId}?assets=0`)).images[0].id;
  const total = (await api(page, "get", "gallery/list?limit=1")).total;
  await stage(frame, [existing, fresh]);
  if (mode === "group") await toggle(frame, "importAsGroup", true);
  if (mode === "merge") await toggle(frame, "importMergeExisting", true);
  const before = { ...traffic };
  const checked = page.waitForResponse((response) => response.url().includes("/imports/check"));
  await frame.locator("#confirmImportButton").click(); const result = await decoded(await checked);
  assert.equal(result.allowed, false); assert.equal(result.code, "gallery_duplicates"); assert.deepEqual(result.duplicate_hashes, [existing.sha256]);
  await frame.locator(".import-card.is-duplicate").waitFor();
  assert.equal(await frame.locator(".import-card.is-duplicate").count(), 1); assert.equal(await frame.locator(".import-card.is-duplicate").getAttribute("data-import-sha256"), existing.sha256);
  assert.match(await frame.locator(".import-card.is-duplicate").textContent(), /画廊中已存在/);
  assert.equal(traffic.prepare, before.prepare); assert.equal(traffic.upload, before.upload); assert.equal(traffic.targets, before.targets);
  assert.equal((await api(page, "get", "gallery/list?limit=1")).total, total);
  await capture(page, inner, `${name}-${mode}-duplicate-batch`);
  await inner.locator(".import-card.is-duplicate .import-card-status").evaluate((element) => element.scrollIntoView({ block: "center" }));
  await capture(page, inner, `${name}-${mode}-duplicate-label`);
  await frame.locator(".import-card.is-duplicate [data-remove-import]").click();
  if (mode === "group") { await stage(frame, [fixture(`${name}-group-extra`)]); await toggle(frame, "importAsGroup", true); }
  if (mode === "merge") await toggle(frame, "importMergeExisting", true);
  const committed = await importComplete(page, frame, mode, targetId); assert.equal(committed.allowed, true);
  const target = await api(page, "get", `gallery/detail/${targetId}?assets=0`);
  assert.equal(target.images[0].id, existingImageId); assert.equal(target.images.length, mode === "merge" ? 2 : 1, "existing gallery images must be preserved");
  assert.equal((await api(page, "get", "gallery/list?limit=1")).total, total + (mode === "merge" ? 0 : 1));
}

async function lateConflict(page, frame, inner, name, traffic) {
  const collision = fixture(`${name}-late-collision`); const companion = fixture(`${name}-late-companion`);
  const total = (await api(page, "get", "gallery/list?limit=1")).total;
  let otherId = ""; let attempted = false; let conflict; let commitPath = "";
  const insertDuringCommit = async (route) => {
    if (attempted) { await route.continue(); return; }
    attempted = true; otherId = await seed(page, collision); commitPath = route.request().url().slice(apiRoot.length + 1);
    const response = await route.fetch(); conflict = await decoded(response); await route.fulfill({ response });
  };
  await stage(frame, [collision, companion]); await page.route("**/imports/group/*/commit", insertDuringCommit);
  await frame.locator("#confirmImportButton").click(); await frame.locator(".import-card.is-duplicate").waitFor();
  assert.equal(conflict.allowed, false); assert.equal(conflict.code, "gallery_duplicates");
  assert.equal((await api(page, "get", "gallery/list?limit=1")).total, total + 1, "a commit-time conflict must refuse the whole pending batch");
  const companionStatus = await api(page, "post", "imports/check", { items: [{ client_id: "late_companion", sha256: companion.sha256 }] }); assert.equal(companionStatus.allowed, true);
  const expired = await page.request.post(`${apiRoot}/${commitPath}`, { data: {} }); assert.ok([404, 410].includes(expired.status()), "conflicting staged batch should be discarded");
  await capture(page, inner, `${name}-late-conflict`); await page.unroute("**/imports/group/*/commit", insertDuringCommit);
  const beforeUploads = traffic.upload;
  await frame.locator(".import-card.is-duplicate [data-remove-import]").click(); const result = await importComplete(page, frame, "separate"); assert.equal(result.allowed, true);
  assert.equal(traffic.upload, beforeUploads + 1, "a cleaned conflict draft must prepare and upload its remaining image again");
  assert.equal((await api(page, "get", `gallery/detail/${otherId}?assets=0`)).images.length, 1);
}

async function lostResponse(page, frame, name, traffic) {
  const image = fixture(`${name}-lost-response`); await stage(frame, [image]);
  let intercepted = false; let committed;
  const lost = async (route) => {
    if (intercepted) { await route.continue(); return; }
    intercepted = true; const response = await route.fetch(); committed = await decoded(response); assert.equal(committed.allowed, true);
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试提交响应丢失，请重试" }) });
  };
  await page.route("**/imports/group/*/commit", lost); await frame.locator("#confirmImportButton").click();
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); await frame.locator("#importProgress").filter({ hasText: "重试" }).waitFor();
  const total = (await api(page, "get", "gallery/list?limit=1")).total; const before = { ...traffic };
  const result = await importComplete(page, frame, "separate"); assert.deepEqual(result.generation_ids, committed.generation_ids);
  assert.equal(traffic.upload, before.upload); assert.equal(traffic.prepare, before.prepare); assert.equal(traffic.check, before.check, "retrying an already committed draft must not duplicate-check itself");
  assert.equal((await api(page, "get", "gallery/list?limit=1")).total, total);
  await page.unroute("**/imports/group/*/commit", lost);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [{ width: 1440, theme: "light" }, { width: 1100, theme: "dark" }, { width: 390, theme: "light" }, { width: 320, theme: "dark" }]) {
      const name = `${test.width}-${test.theme}`; const page = await browser.newPage({ viewport: { width: test.width, height: test.width < 600 ? 844 : 1000 }, hasTouch: test.width < 600 });
      page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); assert.equal(await page.locator("#studio").count(), 1);
      const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const inner = page.frames().find((item) => item.url().includes("/ui/")); await inner.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, test.theme);
      const traffic = { prepare: 0, upload: 0, check: 0, targets: 0 };
      page.on("request", (request) => { for (const [key, suffix] of [["prepare", "/imports/prepare"], ["upload", "/imports/upload/"], ["check", "/imports/check"], ["targets", "/imports/merge-targets"]]) if (request.url().includes(suffix)) traffic[key]++; });
      await frame.locator('[data-view="import"]').click(); await localDedup(page, frame, inner, name);
      for (const mode of ["separate", "group", "merge"]) await duplicateBatch(page, frame, inner, name, mode, traffic);
      await lateConflict(page, frame, inner, name, traffic); await lostResponse(page, frame, name, traffic);
      assert.deepEqual(errors, [], `${name}: page errors`); await page.close(); console.log(`${name}: local hash dedup, fallback, cancellation, all-mode rejection, late conflicts, atomic retry passed`);
    }
    console.log(`Dedup screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
