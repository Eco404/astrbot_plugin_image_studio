/* Delay a thumbnail in the isolated harness to expose its initial paint. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18771";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-gallery-loading-"));

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
      page.on("pageerror", error => errors.push(error.message));
      let thumbnail, release;
      const pending = new Promise(resolve => { release = resolve; });
      await page.route("**/gallery/list?*", async route => {
        const response = await route.fetch(); const body = await response.json(); const payload = body.data || body;
        const dataUrl = payload.items[0].thumbnail_data_url;
        thumbnail = Buffer.from(dataUrl.split(",")[1], "base64");
        payload.items[0].thumbnail_data_url = `${base}/__slow_thumbnail.webp`;
        await route.fulfill({ response, json: body });
      });
      await page.route("**/__slow_thumbnail.webp", async route => { await pending; await route.fulfill({ contentType: "image/webp", body: thumbnail }); });
      await page.goto(base);
      await page.reload();
      const frame = page.frames().find(item => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(theme => { document.documentElement.dataset.theme = theme; }, test.theme);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator(".gallery-card").first().waitFor();
      await frame.evaluate(async () => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));
      const initial = await frame.locator(".gallery-image-wrap").first().evaluate(element => {
        const image = element.querySelector("img"); window.__thumbnailNode = image; window.__cardNode = element.parentElement;
        const styles = getComputedStyle(element); const rect = element.getBoundingClientRect();
        return { background: styles.backgroundColor, surface: styles.getPropertyValue("--surface").trim(), complete: image.complete, loading: image.loading, width: rect.width, height: rect.height };
      });
      assert.equal(initial.complete, false, "thumbnail must be held while inspecting initial paint");
      assert.equal(initial.loading, "eager");
      assert.equal(initial.background, test.theme === "light" ? "rgba(255, 255, 255, 0.45)" : "rgba(255, 255, 255, 0.05)");
      assert.ok(Math.abs(initial.width - initial.height) < 1, "loading placeholder must retain square geometry");
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-loading.png`) });
      release();
      await frame.waitForFunction(() => window.__thumbnailNode.complete && window.__thumbnailNode.naturalWidth > 0);
      const completed = await frame.locator(".gallery-image-wrap").first().evaluate(element => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, same: element.querySelector("img") === window.__thumbnailNode && element.parentElement === window.__cardNode }));
      assert.equal(completed.same, true);
      assert.equal(completed.height, initial.height);
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-loaded.png`) });
      assert.deepEqual(errors, []);
      console.log(`${test.width}-${test.theme}: cold gallery load uses themed placeholder, stable dimensions and eager visible thumbnails`);
      await page.close();
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
