/* Continuous, filtered image cursors use disposable uploads in the isolated harness. */
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
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-continuity-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(30):
 image=Image.new("RGB",(280,360),(110+index*3,200-index*3,180));draw=ImageDraw.Draw(image)
 draw.rectangle((0,250,280,360),fill=(90,110+index*3,125));draw.polygon([(0,250),(110,90),(240,250)],fill=(160,110,140+index*3))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"continuity {marker} image {index}","model":"continuity-model","steps":20+index,"seed":index,"width":280,"height":360,"request_type":"PromptGenerateRequest"}));file=folder/f"{marker}-{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;

async function api(page, method, endpoint, data) {
  const response = await page.request[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const body = await response.json(); return body.data || body;
}

async function seed(page, files, marker, asGroup = false) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "continuity-model" } }));
  const batch = await api(page, "post", "imports/prepare", { items, as_group: asGroup }); assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await page.request.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok()); assert.equal((await response.json()).uploaded, true);
  }
  const result = await api(page, "post", batch.commit_endpoint, {}); assert.equal(result.allowed, true); return result.generation_ids;
}

async function selected(inner, generation, index) {
  await inner.waitForFunction(({ generation, index }) => {
    const frame = document.querySelector(".detail-image-frame");
    return frame?.dataset.generationId === generation && frame.getAttribute("aria-busy") !== "true"
      && frame.querySelector("[data-detail-image]")?.dataset.detailImage === String(index)
      && !frame.dataset.detailSwipeState;
  }, { generation, index });
}

async function watchFrame(inner) {
  await inner.evaluate(() => {
    window.__continuityObserver?.disconnect();
    const frame = document.querySelector(".detail-image-frame");
    window.__continuityNodes = [frame, frame.querySelector("[data-detail-image]"), frame.querySelector(".detail-image-background"), frame.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)")];
    window.__continuityDetached = false;
    window.__continuityObserver = new MutationObserver((records) => {
      for (const record of records) for (const node of record.removedNodes) {
        if (node === frame || (node instanceof Element && node.contains(frame))) window.__continuityDetached = true;
      }
    });
    window.__continuityObserver.observe(document.querySelector("#drawerBody"), { childList: true, subtree: true });
  });
}

async function persistent(inner) {
  assert.equal(await inner.evaluate(() => {
    const frame = document.querySelector(".detail-image-frame");
    const actual = [frame, frame?.querySelector("[data-detail-image]"), frame?.querySelector(".detail-image-background"), frame?.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)")];
    return !window.__continuityDetached && actual.every((node, index) => node === window.__continuityNodes[index]);
  }), true, "cross-group navigation must retain the mounted frame, original and fixed backdrop nodes");
  assert.equal(await inner.locator(".detail-loading").count(), 0, "navigation must not expose a replacement loading view");
}

async function touch(inner, type, dx = 0) {
  await inner.evaluate(({ type, dx }) => {
    const frame = document.querySelector(".detail-image-frame");
    const rect = frame.getBoundingClientRect();
    const point = { identifier: 41, target: frame, clientX: rect.x + rect.width * .5 + dx, clientY: rect.y + Math.min(160, rect.height * .4) };
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [point] }, changedTouches: { value: [point] }, targetTouches: { value: type === "touchend" ? [] : [point] } });
    frame.dispatchEvent(event);
  }, { type, dx });
}

async function swipe(inner, direction, check) {
  await inner.locator("#drawerBody").evaluate((element) => { element.scrollTop = 0; });
  await touch(inner, "touchstart"); await touch(inner, "touchmove", -direction * 90);
  if (check) await check();
  await touch(inner, "touchmove", -direction * 140); await touch(inner, "touchend", -direction * 140);
  await inner.waitForFunction(() => !document.querySelector(".detail-image-frame")?.dataset.detailSwipeState);
}

async function run(browserName, width) {
  const browser = await engines[browserName].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 844 }, hasTouch: width <= 540, deviceScaleFactor: width <= 540 ? 3 : 1 });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  const marker = `${path.basename(output)}-${browserName}-${width}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const created = [];
  let releaseAssets;
  const assetsGate = new Promise((resolve) => { releaseAssets = resolve; });
  const assetRequests = [];
  await page.route("**/gallery/assets/*", async (route) => {
    assetRequests.push(route.request().url().split("/").pop()); await assetsGate;
    try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; }
  });
  try {
    created.push(...await seed(page, files.slice(3, 30), `${marker}-singles`));
    const [group] = await seed(page, files.slice(0, 3), `${marker}-group`, true); created.push(group);
    const sequence = (await api(page, "get", `gallery/image-sequence?query=${encodeURIComponent(marker)}`)).items;
    const groupEnd = sequence.findIndex((item) => item.generation_id === group && item.image_index === 2);
    const neighbor = sequence[groupEnd + 1]; assert.ok(neighbor);
    await page.goto(base); const inner = page.frames().find((item) => item.url().includes("/ui/"));
    await inner.locator("#modelChoice:not(:disabled)").waitFor();
    await inner.locator('[data-view="gallery"]').click();
    await inner.locator("#gallerySearch").fill(marker); await inner.locator("#gallerySearch").press("Tab");
    await inner.locator(`[data-gallery-id="${group}"] .gallery-info`).waitFor();
    await inner.locator(`[data-gallery-id="${group}"] .gallery-info`).click();
    await selected(inner, group, 0);
    await inner.locator('[data-detail-dot="2"]').click(); await selected(inner, group, 2);
    await watchFrame(inner);
    if (width <= 540) {
      await swipe(inner, 1, async () => {
        await inner.locator('.detail-swipe-pane[data-swipe-offset="1"] img').waitFor();
        assert.equal(await inner.locator(".detail-image-frame").getAttribute("data-generation-id"), group, "drag preview must not commit metadata");
        assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), 3);
        assert.ok(assetRequests.every((id) => id === group), "adjacent records must not preload whole original groups");
        await page.screenshot({ path: path.join(output, `${browserName}-${width}-cross-drag.png`) });
      });
    } else await inner.locator('[data-detail-nav="1"]').click();
    await selected(inner, neighbor.generation_id, 0); await persistent(inner);
    assert.equal(await inner.locator(".detail-filmstrip").count(), 0);
    const adjacentSummary = await api(page, "get", `gallery/detail/${neighbor.generation_id}?assets=0`);
    assert.ok((await inner.locator("#drawerBody").textContent()).includes(adjacentSummary.original_prompt), "new image must display its own record parameters");
    if (width <= 540) await swipe(inner, -1); else await inner.locator("#detailDrawer").press("ArrowLeft");
    await selected(inner, group, 2); await persistent(inner);
    assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), 3);
    await inner.locator("#closeDrawer").click();

    const visibleIds = await inner.locator("[data-gallery-id]").evaluateAll((cards) => cards.map((card) => card.dataset.galleryId));
    assert.ok(visibleIds.length < created.length, "fixture must cross an actual gallery page boundary");
    const lastId = visibleIds.at(-1);
    const edgeIndex = sequence.findLastIndex((item) => item.generation_id === lastId);
    const nextPageItem = sequence[edgeIndex + 1]; assert.ok(nextPageItem && !visibleIds.includes(nextPageItem.generation_id));
    await inner.locator(`[data-gallery-id="${lastId}"] .gallery-info`).click(); await selected(inner, lastId, 0); await watchFrame(inner);
    await inner.locator("#detailDrawer").press("ArrowRight");
    await selected(inner, nextPageItem.generation_id, nextPageItem.image_index); await persistent(inner);
    assert.deepEqual(await inner.locator("[data-gallery-id]").evaluateAll((cards) => cards.map((card) => card.dataset.galleryId)), visibleIds, "cross-page detail navigation must not rerender the obscured gallery");
    await inner.locator("#detailDrawer").press("ArrowLeft"); await selected(inner, lastId, 0); await persistent(inner);
    await inner.locator("#closeDrawer").click();

    const failurePath = `**/gallery/detail/${nextPageItem.generation_id}?*`;
    await page.route(failurePath, (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "测试：目标图片暂时无法读取" }) }));
    await inner.locator(`[data-gallery-id="${lastId}"] .gallery-info`).click(); await selected(inner, lastId, 0); await watchFrame(inner);
    if (width <= 540) await swipe(inner, 1); else await inner.locator("#detailDrawer").press("ArrowRight");
    await inner.locator("#appNotice.is-error").waitFor();
    assert.match(await inner.locator("#appNoticeMessage").textContent(), /图片切换失败|目标图片暂时无法读取/);
    await selected(inner, lastId, 0); await persistent(inner);
    await inner.locator("#closeDrawer").click(); await page.unroute(failurePath);

    let releaseTarget; let targetStarted;
    const targetGate = new Promise((resolve) => { releaseTarget = resolve; });
    const started = new Promise((resolve) => { targetStarted = resolve; });
    await page.route(failurePath, async (route) => { targetStarted(); await targetGate; try { await route.continue(); } catch (error) { if (!/handled|closed|disposed/i.test(error.message)) throw error; } });
    try {
      await inner.locator(`[data-gallery-id="${lastId}"] .gallery-info`).click(); await selected(inner, lastId, 0);
      await inner.locator("#detailDrawer").press("ArrowRight"); await started;
      assert.equal(await inner.locator(".detail-image-frame").getAttribute("data-generation-id"), lastId, "pending navigation must retain the origin");
      await inner.locator("#closeDrawer").click();
      await inner.locator(`[data-gallery-id="${group}"] .gallery-info`).click(); await selected(inner, group, 0);
      releaseTarget(); await page.waitForTimeout(450); await selected(inner, group, 0);
      assert.equal(await inner.locator(".detail-filmstrip-thumb").count(), 3, "late requests must not overwrite reopened details");
    } finally { releaseTarget(); await page.unroute(failurePath); }

    const beforeDeletion = await api(page, "get", `gallery/detail/${group}?assets=0`);
    await inner.locator('[data-detail-dot="1"]').click(); await selected(inner, group, 1); await watchFrame(inner);
    const deletion = await api(page, "post", "gallery/images/delete", { generation_id: group, image_ids: [beforeDeletion.images[0].id] });
    assert.equal(deletion.remaining, 2);
    const refresh = page.waitForResponse((response) => response.url().includes("/gallery/image-sequence") && new URL(response.url()).searchParams.get("query") === marker);
    // Simulate an external gallery refresh while the existing detail remains open.
    await inner.locator("#galleryRefresh").evaluate((button) => button.click());
    const refreshedResponse = await refresh; const refreshedBody = await refreshedResponse.json();
    const refreshedItems = (refreshedBody.data || refreshedBody).items.filter((item) => item.generation_id === group);
    assert.deepEqual(refreshedItems.map((item) => item.image_id), beforeDeletion.images.slice(1).map((image) => image.id));
    assert.deepEqual(refreshedItems.map((item) => item.image_index), [0, 1], "server image positions must actually shift after deletion");
    await inner.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (width <= 540) await swipe(inner, 1); else await inner.locator("#detailDrawer").press("ArrowRight");
    await inner.waitForFunction(({ group, source }) => {
      const frame = document.querySelector(".detail-image-frame");
      return frame?.dataset.generationId === group && !frame.dataset.detailSwipeState
        && frame.querySelector("[data-detail-image]")?.src === source;
    }, { group, source: beforeDeletion.images[2].thumbnail_data_url });
    await persistent(inner);
    assert.equal(await inner.locator(".detail-image-frame").getAttribute("data-generation-id"), group, "deleting an earlier image must not skip the next image into another group");
    await page.screenshot({ path: path.join(output, `${browserName}-${width}-settled.png`) });
    assert.deepEqual(errors, [], "browser console exceptions");
    console.log(`${browserName} ${width}: continuous group/page/reverse transitions, persistent image layers, filtered cursors, speculative thumbnail-only loading, failure rollback, stale request cancellation and external-delete cursor stability passed`);
  } finally {
    releaseAssets();
    if (created.length) await api(page, "post", "gallery/delete", { ids: created });
    await page.close(); await browser.close();
  }
}

(async () => {
  for (const [engine, width] of [["chromium", 1440], ["chromium", 390], ["webkit", 390]]) await run(engine, width);
  console.log(`Continuity screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
