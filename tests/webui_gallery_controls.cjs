/* Run against the isolated webui_harness.py, never against deployment data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18765";
const root = path.resolve(__dirname, "..");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-gallery-controls-"));

async function choose(frame, selector, value) {
  const select = frame.locator(selector);
  const index = await select.evaluate((element, desired) => Array.from(element.options).findIndex(option => option.value === desired), value);
  assert.ok(index >= 0, `${selector}: missing ${value}`);
  await select.locator("..").locator(".studio-select-trigger").click();
  await frame.locator(`.studio-select-menu [data-option-index="${index}"]`).click();
  assert.equal(await frame.locator(selector).inputValue(), value);
}

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function checkMenu(frame) {
  await settle(frame);
  const layout = await frame.locator(".studio-select-menu").evaluate(menu => {
    const box = menu.getBoundingClientRect();
    return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: innerWidth, height: innerHeight, selected: menu.querySelectorAll('[aria-selected="true"]').length };
  });
  assert.ok(layout.left >= 7 && layout.top >= 7 && layout.right <= layout.width - 7 && layout.bottom <= layout.height - 7, JSON.stringify(layout));
  assert.equal(layout.selected, 1);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [
      { width: 1440, height: 1000, theme: "light" },
      { width: 900, height: 1000, theme: "dark" },
      { width: 390, height: 844, theme: "light" },
      { width: 360, height: 800, theme: "dark" },
    ]) {
      const page = await browser.newPage({ viewport: { width: test.width, height: test.height }, hasTouch: test.width < 600 });
      page.setDefaultTimeout(15000);
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.goto(base);
      const frame = page.frames().find(item => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(theme => { document.documentElement.dataset.theme = theme; }, test.theme);

      await choose(frame, "#modelChoice", "natural:studio-image");
      await choose(frame, '[data-model-parameter="size"]', "1024x1536");
      await frame.locator('[data-mode="img2img"]').click();
      assert.equal(await frame.locator('[data-model-parameter="size"]').inputValue(), "1024x1536");
      assert.ok((await frame.locator('[data-model-parameter="size"]').locator("..").innerText()).includes("1024x1536"));
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator(".gallery-card").first().waitFor();
      await settle(frame);
      await frame.evaluate(() => {
        window.__galleryNodes = Array.from(document.querySelectorAll(".gallery-card"));
        window.__galleryImages = window.__galleryNodes.map(card => card.querySelector("img"));
      });
      for (let index = 0; index < 2; index++) {
        const response = page.waitForResponse(item => item.url().includes("/gallery/list?"));
        await frame.locator("#galleryRefresh").click(); await response; await settle(frame);
        const preserved = await frame.evaluate(() => Array.from(document.querySelectorAll(".gallery-card")).every((card, index) => card === window.__galleryNodes[index] && card.querySelector("img") === window.__galleryImages[index] && getComputedStyle(card).animationName === "none"));
        assert.equal(preserved, true, "refresh should preserve cards and decoded images without replaying animation");
      }
      const engineValues = await frame.locator("#galleryEngine").evaluate(select => Array.from(select.options).map(option => option.value));
      assert.ok(engineValues.includes("novelai") && !engineValues.includes("nai"));
      await frame.locator('.studio-select-trigger[data-select-id="galleryEngine"]').click();
      await checkMenu(frame);
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-gallery-menu.png`) });
      await frame.locator('.studio-select-trigger[data-select-id="galleryEngine"]').press("Escape");
      assert.equal(await frame.locator(".studio-select-menu").count(), 0);

      await frame.locator(".gallery-card .gallery-info").first().click();
      await frame.locator(".detail-parameter-grid").first().waitFor();
      const columns = await frame.locator(".detail-parameter-grid").first().evaluate(element => getComputedStyle(element).columnCount);
      assert.equal(columns, test.width <= 540 ? "1" : "2");
      await frame.locator("#drawerBody").evaluate(element => { element.scrollTop = 280; });
      await settle(frame);
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-parameters.png`) });
      await frame.locator('.studio-select-trigger[data-select-id="detailCopyFormat"]').click();
      await checkMenu(frame);
      if (test.width <= 540) {
        const trigger = await frame.locator('.studio-select-trigger[data-select-id="detailCopyFormat"]').boundingBox();
        assert.equal(trigger.width, 44); assert.equal(trigger.height, 44);
      }
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-detail-menu.png`) });
      await frame.locator('.studio-select-trigger[data-select-id="detailCopyFormat"]').press("Escape");
      assert.ok(await frame.locator("#detailDrawer.is-open").isVisible(), "Escape should close the select before the dialog");
      await frame.locator("#closeDrawer").click();

      await frame.locator('[data-view="settings"]').click();
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
      await frame.locator('[data-settings-provider="natural"]').click();
      await frame.locator('[data-model-tab="tool"]').click();
      await frame.locator('[data-edit-tool-parameter="quality"]').click();
      await frame.locator('.studio-select-trigger[data-select-id="toolParameterDefaultChoice"]').click();
      await checkMenu(frame);
      await frame.locator('.studio-select-trigger[data-select-id="toolParameterDefaultChoice"]').press("Escape");
      assert.ok(await frame.locator("#parameterDialog:not(.is-hidden)").isVisible());
      await frame.locator("#parameterDialog").press("Escape");
      await frame.locator('[data-settings-provider="nai"]').click();
      await frame.locator("#newModelChoice").click();
      await frame.locator("#newModelChoice").fill("nai-diffusion-4");
      await frame.locator(".studio-select-menu").waitFor();
      await settle(frame);
      await page.screenshot({ path: path.join(output, `${test.width}-${test.theme}-model-options.png`) });
      await frame.locator("#newModelChoice").press("Escape");
      await frame.locator("#newModelChoice").fill("custom-model-id");
      await frame.locator("#newModelChoice").press("Escape");
      assert.equal(await frame.locator("#newModelChoice").inputValue(), "custom-model-id");
      assert.equal(await frame.locator("#newModelChoice").getAttribute("list"), null);

      const sample = path.join(root, "data/image/149037466_p0.webp");
      if (fs.existsSync(sample)) {
        await frame.locator('[data-view="import"]').click();
        const buffer = execFileSync(process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python", ["-c", "import sys,io; from PIL import Image,PngImagePlugin; image=Image.open(sys.argv[1]); metadata=PngImagePlugin.PngInfo(); [metadata.add_text(k,v) for k,v in image.info.items() if isinstance(v,str)]; metadata.add_text('BrowserFixture',sys.argv[2]); output=io.BytesIO(); image.save(output,format='PNG',pnginfo=metadata,exif=image.info.get('exif',b'')); sys.stdout.buffer.write(output.getvalue())", sample, `${path.basename(output)}-${test.width}-${test.theme}`], { maxBuffer: 32 * 1024 * 1024 });
        await frame.locator("#importFiles").setInputFiles({ name: `converted-${test.width}.png`, mimeType: "image/png", buffer });
        await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
        assert.equal(await frame.locator('[data-import-field="generation_engine"]').inputValue(), "comfyui");
        assert.equal(await frame.locator('[data-import-field="model"]').inputValue(), "anima_baseV10.safetensors");
        assert.ok((await frame.locator(".import-card-status").innerText()).includes("已识别 ComfyUI"));
        await choose(frame, '[data-import-field="mode"]', "text2img");
        await frame.locator("#confirmImportButton").click();
        await frame.locator("#importProgress").filter({ hasText: "已导入" }).waitFor();
      }
      const geometry = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      assert.ok(geometry.scroll <= geometry.width + 1, JSON.stringify(geometry));
      assert.deepEqual(errors, []);
      console.log(`${test.width}-${test.theme}: stable gallery, masonry, themed controls, modal focus and WebP import passed`);
      await page.close();
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
