/* Real WebUI/API/service/store flow against webui_harness.py's fake provider. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-batch-service-"));

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.setDefaultTimeout(15000);
  try {
    const home = await page.request.get(base);
    assert.match(await home.text(), /<iframe id="studio"/, "only use the isolated harness");
    const before = await (await page.request.get(`${apiRoot}/gallery/list?limit=1`)).json();
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator("#modelChoice").evaluate(input => {
      input.value = "nai:nai-diffusion-4-5-full";
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await frame.locator("#prompt").fill("mountain landscape, daylight, isolated batch service test");
    await frame.locator('[data-model-parameter="count"]').fill("3");
    assert.equal(await frame.locator('[data-model-parameter="concurrency"]').count(), 0);
    const generated = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/studio/generate"));
    await frame.locator("#generateButton").click();
    const response = await generated;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.images.length, 3, "service must combine three one-image provider requests");
    assert.equal(result.warning || "", "");
    assert.ok(result.generation_id);
    await frame.waitForFunction(() => document.querySelectorAll(".result-card").length === 3);
    await frame.locator("#resultMeta").filter({ hasText: "已保存到画廊" }).waitFor();
    const after = await (await page.request.get(`${apiRoot}/gallery/list?limit=1`)).json();
    assert.equal(after.total, before.total + 1, "one batch creates one gallery record");
    assert.equal(after.items[0].id, result.generation_id);
    const detail = await (await page.request.get(`${apiRoot}/gallery/detail/${result.generation_id}`)).json();
    assert.equal(detail.images.length, 3, "the record must retain the whole batch");
    assert.equal(new Set(detail.images.map(image => image.id)).size, 3, "image relationships remain individually addressable");
    await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await frame.locator('[data-view="gallery"]').click();
    const card = frame.locator(`.gallery-card[data-gallery-id="${result.generation_id}"]`);
    await card.waitFor();
    await card.click();
    await frame.waitForFunction(() => document.querySelectorAll(".detail-filmstrip-thumb").length === 3);
    await page.screenshot({ path: path.join(output, "batch-detail.png") });
    assert.deepEqual(errors, []);
    console.log(`Real WebUI -> API -> service -> fake provider -> gallery passed: ${result.generation_id}, 3 images, 1 record`);
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
