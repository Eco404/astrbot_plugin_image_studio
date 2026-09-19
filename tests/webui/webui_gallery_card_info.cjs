/* Browser-owned gallery display preferences follow the settings draft lifecycle. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-gallery-card-info-"));

async function verify(engine, name, width) {
  const browser = await engine.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 940 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let frame;
  async function loaded() {
    await page.locator("#studio").waitFor();
    frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.waitForURL(/\/ui\//);
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
  }
  async function settings() {
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#galleryCardInfo").waitFor({ state: "attached" });
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  }
  async function toggle() {
    await frame.locator('label[for="galleryCardInfo"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "有未保存" }).waitFor();
  }
  async function save() {
    await frame.locator("#saveSettingsButton").click();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  }
  const firstCard = () => frame.locator("#galleryGrid [data-gallery-id]").first();
  try {
    await page.goto(base); await loaded();
    await frame.locator('[data-view="gallery"]').click();
    await firstCard().locator(".gallery-info").waitFor();
    const before = await firstCard().boundingBox();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().galleryCardInfo), true);

    await settings();
    assert.equal(await frame.locator("#galleryCardInfo").isChecked(), true);
    await toggle();
    assert.equal(await frame.evaluate(() => document.documentElement.dataset.galleryCardInfo), "false");
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#staySettingsButton").click();
    assert.equal(await frame.locator("#settingsView").evaluate(node => node.classList.contains("is-active")), true);
    assert.equal(await frame.locator("#galleryCardInfo").isChecked(), false);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#discardSettingsButton").click();
    await firstCard().locator(".gallery-info").waitFor();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().galleryCardInfo), true);

    await settings(); await toggle(); await save();
    await frame.locator('[data-view="gallery"]').click();
    await firstCard().waitFor();
    assert.equal(await firstCard().locator(".gallery-info").isVisible(), false);
    assert.equal(await firstCard().locator(".gallery-source-label").isVisible(), true);
    assert.equal(await firstCard().locator(".gallery-selection").isVisible(), true);
    const after = await firstCard().boundingBox();
    assert.ok(after.height < before.height - 30, "hidden information must not reserve a blank area");
    await firstCard().locator('input[type="checkbox"]').check({ force: true });
    assert.equal(await firstCard().evaluate(node => node.classList.contains("is-selected")), true);
    await firstCard().locator('input[type="checkbox"]').uncheck({ force: true });
    await firstCard().locator(".gallery-image-wrap").click();
    await frame.locator("#detailDrawer.is-open").waitFor();
    await frame.locator("#closeDrawer").click();
    await frame.waitForFunction(() => !document.getElementById("detailDrawer").classList.contains("is-open"));
    await page.screenshot({ path: path.join(output, `${name}-${width}-images-only.png`) });

    await page.reload(); await loaded();
    await frame.locator('[data-view="gallery"]').click();
    await firstCard().waitFor();
    assert.equal(await firstCard().locator(".gallery-info").isVisible(), false, "reload must restore the saved display preference");
    await settings();
    assert.equal(await frame.locator("#galleryCardInfo").isChecked(), false);
    await frame.locator(".appearance-reset").click();
    await frame.locator('[data-appearance-reset="confirm"]').click();
    assert.equal(await frame.locator("#galleryCardInfo").isChecked(), false, "resetting the theme does not overwrite gallery display preferences");
    await toggle(); await save();
    await frame.locator('[data-view="gallery"]').click();
    await firstCard().locator(".gallery-info").waitFor();
    await page.screenshot({ path: path.join(output, `${name}-${width}-information.png`) });
    assert.deepEqual(errors, []);
    console.log(`${name} ${width}: gallery information toggle, draft guard, discard, save, reload and card actions passed`);
  } finally { await browser.close(); }
}

(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    for (const width of [390, 1440]) await verify(engine, name, width);
  }
  console.log(`Gallery card display screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
