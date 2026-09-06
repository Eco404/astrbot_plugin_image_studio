/* Run against tests/webui_harness.py, never against deployment data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const root = path.resolve(__dirname, "..");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18765";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-browser-"));

async function checkGeometry(frame) {
  const value = await frame.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  assert.ok(value.scroll <= value.width + 1, JSON.stringify(value));
}

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
  });
}

async function opened(browser, test) {
  const page = await browser.newPage({ viewport: test.viewport, hasTouch: test.viewport.width < 600 });
  page.setDefaultTimeout(12000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(base);
  const frame = page.frameLocator("#studio");
  await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
  const inner = page.frames().find(item => item.url().includes("/ui/"));
  await inner.evaluate(async theme => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: theme }); }, test.theme);
  return { page, frame, inner, errors };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [
      { name: "desktop-light", viewport: { width: 1440, height: 1000 }, theme: "light" },
      { name: "compact-dark", viewport: { width: 1100, height: 900 }, theme: "dark" },
      { name: "tablet-light", viewport: { width: 900, height: 1000 }, theme: "light" },
      { name: "tablet-dark", viewport: { width: 720, height: 1212 }, theme: "dark" },
      { name: "mobile-light", viewport: { width: 390, height: 844 }, theme: "light" },
      { name: "narrow-dark", viewport: { width: 360, height: 800 }, theme: "dark" },
    ]) {
      const { page, frame, inner, errors } = await opened(browser, test);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator(".gallery-card").first().waitFor();
      await checkGeometry(inner);
      const layout = await inner.evaluate(() => ({
        cards: document.querySelectorAll(".gallery-card").length,
        columns: getComputedStyle(document.getElementById("galleryGrid")).gridTemplateColumns.split(/\s+/).length,
      }));
      assert.equal(layout.cards % layout.columns, 0, `${test.name}: incomplete row`);
      assert.ok(layout.cards >= 24);
      await settle(inner);
      await page.screenshot({ path: path.join(output, `${test.name}-gallery.png`) });
      await page.screenshot({ path: path.join(output, `${test.name}-gallery-full.png`), fullPage: true });
      await frame.locator(".gallery-selection").first().click();
      assert.equal(await frame.locator(".gallery-selection input:checked").count(), 1);
      await inner.locator(".gallery-info").first().evaluate(item => item.scrollIntoView({ block: "center" }));
      await frame.locator(".gallery-card .gallery-info").first().click();
      await frame.locator("#detailFooter button:not(:disabled)").first().waitFor();
      await frame.locator("[data-copy-field]").first().waitFor();
      await settle(inner);
      const footer = await frame.locator("#detailFooter").boundingBox();
      await inner.locator("#drawerBody").evaluate(el => { el.scrollTop = el.scrollHeight; });
      const after = await frame.locator("#detailFooter").boundingBox();
      assert.ok(Math.abs(footer.y - after.y) < 1, "detail footer must not scroll");
      assert.ok(after.y + after.height <= test.viewport.height + 1);
      await checkGeometry(inner);
      await settle(inner);
      await page.screenshot({ path: path.join(output, `${test.name}-detail.png`) });
      await frame.locator("#detailDelete").click();
      await frame.locator("#studioModalRoot:not(.is-hidden)").waitFor();
      assert.equal(await frame.locator("[data-delete-image]").count(), 0, "single image must not show selection");
      await frame.locator("#studioModalClose").click();
      if (test.viewport.width < 600) {
        await inner.evaluate(() => {
          const Original = window.PhotoSwipe;
          window.PhotoSwipe = class extends Original {
            constructor(options) { super(options); window.__testPhotoSwipe = this; }
          };
        });
        await inner.locator("#drawerBody").evaluate(el => { el.scrollTop = 0; });
        await frame.locator("[data-detail-image]").click();
        await frame.locator(".pswp--open.pswp--ui-visible").waitFor();
        await inner.waitForFunction(() => window.__testPhotoSwipe?.opener.isOpen);
        await frame.locator(".pswp--open").press("Escape");
        await frame.locator(".pswp--open").waitFor({ state: "detached" });
      }
      await frame.locator("#closeDrawer").click();
      await frame.locator('[data-view="settings"]').click();
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
      const before = await frame.locator("#historyRecords").inputValue();
      await frame.locator("#historyRecords").fill(String(Number(before) + 1));
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "未保存" }).waitFor();
      await frame.locator('[data-view="generate"]').click();
      await frame.locator('[data-view="settings"]').click();
      assert.equal(await frame.locator("#historyRecords").inputValue(), String(Number(before) + 1));
      await checkGeometry(inner);
      await settle(inner);
      await page.screenshot({ path: path.join(output, `${test.name}-settings.png`) });
      await frame.locator("#historyRecords").fill(before);
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
      await frame.locator('[data-view="generate"]').click();
      await frame.locator("#pasteParametersButton").click();
      await frame.locator("#pasteParametersInput").fill(JSON.stringify({
        format: "image_studio", version: 1, generation_engine: "nai",
        data: { model_ref: "nai:nai-diffusion-4-5-full", mode: "text2img", prompt: "mountain, daylight", negative_prompt: "", parameters: { cfg: 0, artist: "", steps: 20 } },
      }));
      await frame.getByRole("button", { name: "读取参数", exact: true }).click();
      await frame.locator("#modelChoice").filter({ has: frame.locator('option:checked[value="nai:nai-diffusion-4-5-full"]') }).waitFor();
      assert.equal(await frame.locator('[data-model-parameter="cfg"]').inputValue(), "0");
      assert.equal(await frame.locator("#negativePrompt").inputValue(), "");
      assert.equal(await frame.locator("#prompt").inputValue(), "mountain, daylight");
      await checkGeometry(inner);
      assert.deepEqual(errors, [], `${test.name}: page errors`);
      await page.close();
      console.log(`${test.name}: gallery, detail, dirty state, paste passed`);
    }

    const { page, frame, inner, errors } = await opened(browser, { viewport: { width: 1440, height: 1000 }, theme: "light" });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card").first().waitFor();
    const selectedId = await frame.locator(".gallery-card").first().getAttribute("data-gallery-id");
    await frame.locator(".gallery-selection").first().click();
    await page.setViewportSize({ width: 1100, height: 900 });
    await inner.waitForFunction(() => document.querySelectorAll(".gallery-card").length === 24);
    assert.equal(await frame.locator(`[data-select-id="${selectedId}"]`).isChecked(), true);
    await frame.locator("#cancelSelectionButton").click();
    await frame.locator("#galleryNext").click();
    await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
    assert.equal(await frame.locator(".has-cleanup-warning").count(), 10);
    const listing = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=60`)).json();
    const multi = (listing.data || listing).items.find(item => item.image_count === 3);
    assert.ok(multi);
    await frame.locator(`[data-gallery-id="${multi.id}"] .gallery-info`).click();
    await frame.locator('[data-detail-dot="1"]').click();
    await settle(inner);
    await frame.locator("#detailFavorite").click();
    await frame.locator('#detailFavorite[aria-pressed="true"]:not(:disabled)').waitFor();
    await frame.locator("#detailDelete").click();
    assert.equal(await frame.locator("[data-delete-image]:checked").count(), 3);
    await frame.locator("[data-delete-image]").nth(1).uncheck();
    await frame.locator("[data-delete-image]").nth(2).uncheck();
    await frame.locator("#deleteImagesAccept").click();
    await frame.locator("#studioModalRoot.is-hidden").waitFor({ state: "attached" });
    const remainingBody = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/detail/${multi.id}?assets=0`)).json();
    const remaining = remainingBody.data || remainingBody;
    assert.equal(remaining.images.length, 2);
    assert.equal(remaining.parameters.count, 3);
    assert.equal(remaining.is_favorite, true);
    await frame.locator("#closeDrawer").click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    const calls = [];
    page.on("request", request => { if (request.url().includes("/imports/")) calls.push(request.url()); });
    await frame.locator('[data-view="import"]').click();
    const files = ["20260831162558_t2i_nai-diffusion-4-5-full.png", "Anima_00001_.png", "Stable Diffusion 149299935.webp"].map(name => path.join(root, "data/image", name));
    if (files.every(file => fs.existsSync(file))) {
      const testFiles = files.map((file, index) => ({ name: `unique-browser-${index}.png`, mimeType: "image/png", buffer: execFileSync(process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python", ["-c", "import sys,io; from PIL import Image,PngImagePlugin; image=Image.open(sys.argv[1]); metadata=PngImagePlugin.PngInfo(); [metadata.add_text(k,v) for k,v in image.info.items() if isinstance(v,str)]; metadata.add_text('BrowserFixture',sys.argv[2]); output=io.BytesIO(); image.save(output,format='PNG',pnginfo=metadata,exif=image.info.get('exif',b'')); sys.stdout.buffer.write(output.getvalue())", file, `${path.basename(output)}-${index}`], { maxBuffer: 32 * 1024 * 1024 }) }));
      await frame.locator("#importFiles").setInputFiles(testFiles);
      await frame.locator(".import-card").nth(2).waitFor();
      await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
      assert.equal(calls.filter(url => url.includes("/upload/")).length, 0, "no image uploads before confirmation");
      const engines = await frame.locator('[data-import-field="generation_engine"]').evaluateAll(items => items.map(item => item.value));
      assert.deepEqual(engines, ["novelai", "comfyui", "a1111"]);
      const malicious = 'model" autofocus onfocus="window.__metadataExecuted=true';
      await frame.locator('[data-import-field="model"]').first().fill(malicious);
      // A second batch redraws existing cards with the edited model attribute.
      await frame.locator("#importFiles").setInputFiles(testFiles.slice(0, 1));
      await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
      assert.equal(await frame.locator(".import-card").count(), 3, "reselecting the same bytes must not add another card");
      assert.equal(await frame.locator('[data-import-field="model"]').first().inputValue(), malicious);
      assert.equal(await inner.evaluate(() => !!window.__metadataExecuted), false);
      await frame.locator('.import-card [data-import-field="prompt"]').first().fill("手动修改的导入提示词");
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" });
      assert.equal(calls.filter(url => url.includes("/upload/")).length, 3);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#gallerySource").selectOption("import");
      await frame.locator(".gallery-card").filter({ hasText: "手动修改" }).waitFor();
      await checkGeometry(inner);
      await frame.locator("#galleryEngine").selectOption("comfyui");
      await frame.locator(".gallery-card").first().waitFor();
      await frame.locator(".gallery-card .gallery-info").first().click();
      await frame.locator("#detailCopyFormat").selectOption("workflow");
      const downloadPromise = page.waitForEvent("download");
      await frame.locator("#detailWorkflowDownload").click();
      const download = await downloadPromise;
      const downloaded = path.join(output, download.suggestedFilename());
      await download.saveAs(downloaded);
      const workflow = JSON.parse(fs.readFileSync(downloaded, "utf8"));
      assert.ok(Array.isArray(workflow.nodes));
      await page.setViewportSize({ width: 360, height: 800 });
      await settle(inner);
      await checkGeometry(inner);
      const footer = await frame.locator("#detailFooter").boundingBox();
      assert.ok(footer.x + footer.width <= 361);
      assert.ok(await frame.locator("#detailUseReference").isVisible());
      await frame.locator("#appNoticeClose").click();
      await page.screenshot({ path: path.join(output, "mobile-comfy-detail.png") });
    }
    assert.deepEqual(errors, []);
    await page.close();
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
