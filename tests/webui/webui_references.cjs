/* Use an isolated webui_harness.py instance; no provider requests are sent. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18771";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-references-"));

async function settle(frame) {
  await frame.evaluate(async () => Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))));
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const test of [
      { width: 1440, height: 1000, theme: "light" },
      { width: 390, height: 844, theme: "light" },
      { width: 360, height: 800, theme: "dark" },
    ]) {
      const page = await browser.newPage({ viewport: { width: test.width, height: test.height } });
      const errors = [];
      const uploads = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("request", request => { if (request.url().endsWith("/studio/reference/upload")) uploads.push(request); });
      await page.goto(base);
      const frame = page.frames().find(item => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(async theme => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme);
      await frame.locator('[data-mode="img2img"]').click();
      const button = frame.locator("#referenceChooseButton");
      const count = frame.locator("#referenceCount");
      assert.equal(await count.innerText(), "0/4 张");
      assert.equal(await frame.locator("#referenceUpload").isVisible(), false);
      assert.equal(await button.isEnabled(), true);
      const listing = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=1`)).json();
      const imageId = (listing.data || listing).items[0].image_id;
      const buffer = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/download/${imageId}`)).body();
      const files = Array.from({ length: 7 }, (_, index) => ({ name: `reference-${index}.png`, mimeType: "image/png", buffer }));
      const chooserPromise = page.waitForEvent("filechooser");
      await button.click();
      await (await chooserPromise).setFiles(files);
      await frame.waitForFunction(() => document.getElementById("referenceCount").textContent === "4/4 张" && document.getElementById("referenceChooseButton").getAttribute("aria-busy") === "false");
      assert.equal(uploads.length, 4, "excess files must never be uploaded");
      assert.equal(await button.isDisabled(), true);
      assert.equal(await frame.locator("#referenceUpload").isDisabled(), true);
      assert.equal(await frame.locator(".reference-item").count(), 4);
      await frame.locator("#referenceChooseButton").scrollIntoViewIfNeeded();
      await settle(frame);
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-full.png`) });

      await frame.locator("[data-reference-index]").first().click();
      assert.equal(await count.innerText(), "3/4 张");
      assert.equal(await button.isEnabled(), true);
      await frame.locator("#referenceUpload").setInputFiles(files.slice(0, 3));
      await frame.waitForFunction(() => document.getElementById("referenceCount").textContent === "4/4 张" && document.getElementById("referenceChooseButton").getAttribute("aria-busy") === "false");
      assert.equal(uploads.length, 5, "only the free slot should be uploaded");

      for (let index = 0; index < 4; index++) await frame.locator("[data-reference-index]").first().click();
      let attempt = 0;
      await page.route("**/studio/reference/upload", async route => {
        if (++attempt === 2) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试上传失败" }) });
        else await route.continue();
      });
      await frame.locator("#referenceUpload").setInputFiles(files.slice(0, 3));
      await frame.locator("#generationError").filter({ hasText: "测试上传失败" }).waitFor();
      assert.equal(await count.innerText(), "1/4 张");
      assert.equal(await frame.locator(".reference-item").count(), 1);
      assert.equal(await button.isEnabled(), true, "partial failure must release the uploader");
      await page.unroute("**/studio/reference/upload");
      await frame.locator("[data-reference-index]").first().click();

      let releaseUpload;
      const pending = new Promise(resolve => { releaseUpload = resolve; });
      const uploadStarted = page.waitForRequest(request => request.url().endsWith("/studio/reference/upload"));
      await page.route("**/studio/reference/upload", async route => { await pending; await route.continue(); });
      await frame.locator("#referenceUpload").setInputFiles(files.slice(0, 3));
      await frame.locator('#referenceChooseButton[aria-busy="true"]').waitFor();
      assert.equal(await button.isDisabled(), true, "concurrent selections must be disabled");
      await uploadStarted;
      const requestsBefore = uploads.length;
      await frame.locator('[data-mode="text2img"]').click();
      releaseUpload();
      await frame.waitForFunction(() => document.getElementById("referenceChooseButton").getAttribute("aria-busy") === "false");
      await page.unroute("**/studio/reference/upload");
      assert.equal(uploads.length, requestsBefore, "switching mode should stop the remaining queued uploads");
      await frame.locator('[data-mode="img2img"]').click();
      assert.equal(await count.innerText(), "0/4 张", "late responses must not add stale references");
      assert.equal(await button.isEnabled(), true);
      const geometry = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      assert.ok(geometry.scroll <= geometry.width + 1, JSON.stringify(geometry));
      assert.deepEqual(errors, []);
      console.log(`${test.width}-${test.theme}: themed file chooser, truncation, capacity, deletion, failure and late response passed`);
      await page.close();
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
