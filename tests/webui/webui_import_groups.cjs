/* Use the isolated webui_harness.py service, not a deployment with real data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18765";
const prefix = base + "/astrbot_plugin_image_studio/";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-import-groups-"));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [
      { width: 1440, height: 1000, theme: "light" },
      { width: 1100, height: 900, theme: "dark" },
      { width: 390, height: 844, theme: "light" },
      { width: 360, height: 800, theme: "dark" },
    ]) {
      const page = await browser.newPage({ viewport: { width: test.width, height: test.height } });
      const errors = [];
      const prepares = [];
      const uploads = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("request", request => {
        if (request.url().endsWith("/imports/prepare")) prepares.push(request.url());
        if (request.url().includes("/imports/upload/")) uploads.push(request.url());
      });
      await page.goto(base);
      const frame = page.frames().find(item => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(async theme => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme);
      const before = await (await page.request.get(prefix + "gallery/list?limit=60")).json();
      const originals = before.items.filter(item => item.source !== "import").slice(0, 2);
      const buffers = [];
      for (const item of originals) {
        const original = await (await page.request.get(prefix + `gallery/download/${item.image_id}`)).body();
        const unique = execFileSync(process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python", ["-c", "import sys,io; from PIL import Image,PngImagePlugin; image=Image.open(io.BytesIO(sys.stdin.buffer.read())); metadata=PngImagePlugin.PngInfo(); [metadata.add_text(k,v) for k,v in image.info.items() if isinstance(v,str)]; metadata.add_text('BrowserFixture',sys.argv[1]); output=io.BytesIO(); image.save(output,format='PNG',pnginfo=metadata); sys.stdout.buffer.write(output.getvalue())", `${path.basename(output)}-${test.width}-${item.id}`], { input: original });
        buffers.push(unique);
      }
      await frame.locator('[data-view="import"]').click();
      await frame.locator("#importFiles").setInputFiles(buffers.map((buffer, index) => ({ name: `group-${index}.png`, mimeType: "image/png", buffer })));
      await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
      await frame.locator("#importGroupOption .toggle-control").click();
      assert.equal(await frame.locator("#importAsGroup").isChecked(), true);
      const models = frame.locator('[data-import-field="model"]');
      await models.nth(0).fill("studio-image");
      await models.nth(1).fill("different-model");
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#appNoticeMessage").filter({ hasText: "模型必须相同" }).waitFor();
      assert.equal(prepares.length, 0);
      assert.equal(uploads.length, 0);
      await models.nth(1).fill("");
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#appNoticeMessage").filter({ hasText: "先填写" }).waitFor();
      assert.equal(prepares.length, 0);
      await models.nth(1).fill(" studio-image ");
      const prompts = frame.locator('[data-import-field="prompt"]');
      const marker = `${path.basename(output)}-${test.width}-${test.theme}`;
      await prompts.nth(0).fill(`${marker} first prompt`);
      await prompts.nth(1).fill(`${marker} second prompt`);
      await frame.locator("#appNoticeClose").click();
      await frame.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, `${marker}-import.png`) });
      let seen = 0;
      await page.route("**/imports/upload/*", async route => {
        seen++;
        if (seen === 2) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "测试上传临时失败" }) });
        else await route.continue();
      });
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#importProgress").filter({ hasText: "临时失败" }).waitFor();
      assert.equal(await frame.locator(".import-card").count(), 2);
      assert.equal((await (await page.request.get(prefix + "gallery/list")).json()).total, before.total);
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#importProgress").filter({ hasText: "已导入 1 个图组" }).waitFor();
      assert.equal(prepares.length, 1);
      assert.equal(uploads.length, 3, "retry should not re-upload the successful first image");
      assert.equal(await frame.locator(".import-card").count(), 0);
      const listing = await (await page.request.get(prefix + "gallery/list?source=import&query=" + marker)).json();
      assert.equal(listing.total, 1);
      assert.equal(listing.items[0].image_count, 2);
      const identifier = listing.items[0].id;
      const detail = await (await page.request.get(prefix + `gallery/detail/${identifier}?assets=0`)).json();
      assert.equal(detail.images[0].supplemental.prompt, `${marker} first prompt`);
      assert.equal(detail.images[1].supplemental.prompt, `${marker} second prompt`);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#gallerySearch").fill(marker);
      await frame.locator("#gallerySearch").press("Enter");
      await frame.locator("#galleryRefresh").click();
      await frame.locator(`[data-gallery-id="${identifier}"]`).waitFor();
      await frame.locator(`[data-gallery-id="${identifier}"] .gallery-info`).click();
      await frame.locator(".detail-parameter-row").filter({ hasText: `${marker} first prompt` }).waitFor();
      await frame.locator('[data-detail-dot="1"]').click();
      await frame.locator(".detail-parameter-row").filter({ hasText: `${marker} second prompt` }).waitFor();
      const geometry = await frame.evaluate(() => ({ w: document.documentElement.clientWidth, s: document.documentElement.scrollWidth }));
      assert.ok(geometry.s <= geometry.w + 1);
      assert.deepEqual(errors, []);
      console.log(`${marker}: validation, atomic import, retry and per-image details passed`);
      await page.close();
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
