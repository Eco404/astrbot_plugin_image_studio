/* Selection styling against the isolated harness; never commits imports/deletions. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const browsers = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-selection-colors-"));

async function color(frame, mode, values) {
  await frame.evaluate(async ({ mode, values }) => {
    window.ImageStudioAppearance.set({ preference: mode, ...values });
    await window.ImageStudioAppearance.saved();
  }, { mode, values });
  return frame.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d"); context.fillStyle = document.getElementById("appearanceColor").value; context.fillRect(0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data];
  });
}

async function painted(locator, property, expected, label, pseudo = null) {
  const actual = await locator.evaluate(async (element, { property, pseudo }) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => {})));
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    context.fillStyle = getComputedStyle(element, pseudo)[property]; context.fillRect(0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data];
  }, { property, pseudo });
  assert.deepEqual(actual, expected, label);
}

async function rawShadow(locator, expected) {
  const styles = await locator.evaluate((element) => ({ shadow: getComputedStyle(element).boxShadow, color: getComputedStyle(element).borderTopColor }));
  assert.notEqual(styles.shadow, "none", "selected image/card should have a visible outline shadow");
  assert.ok(styles.shadow.includes(`rgb(${expected.slice(0, 3).join(", ")})`), `selected shadow did not use raw source color: ${styles.shadow}`);
}

async function tickContrast(locator) {
  return locator.evaluate((element) => {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    const luminance = (value) => { context.clearRect(0, 0, 1, 1); context.fillStyle = value; context.fillRect(0, 0, 1, 1); return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).map((v) => { const s = v / 255; return s <= .04045 ? s / 12.92 : ((s + .055) / 1.055) ** 2.4; }).reduce((total, v, index) => total + v * [.2126, .7152, .0722][index], 0); };
    const style = getComputedStyle(element), fill = luminance(style.backgroundColor), foreground = luminance(style.color);
    return (Math.max(fill, foreground) + .05) / (Math.min(fill, foreground) + .05);
  });
}

async function gallery(frame, page, expected) {
  await frame.locator('[data-view="gallery"]').click();
  await frame.locator(".gallery-card").first().waitFor();
  const card = frame.locator(".gallery-card").first();
  await card.locator(".gallery-selection").click();
  await painted(card.locator(".gallery-selection > span"), "backgroundColor", expected, "gallery checkbox changed source color");
  await painted(card, "outlineColor", expected, "selected gallery image outline changed source color");
  const contrast = await tickContrast(card.locator(".gallery-selection > span"));
  assert.ok(contrast >= 4.5, `gallery tick contrast ${contrast}`);
  await frame.locator("#cancelSelectionButton").click();
  await frame.locator('.studio-select-trigger[data-select-id="gallerySource"]').click();
  const mark = frame.locator('.studio-select-option[aria-selected="true"] .studio-select-mark').first();
  await mark.waitFor();
  const dropdownColors = await mark.evaluate(async (element) => {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    const rgba = (color) => { context.clearRect(0, 0, 1, 1); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return [...context.getImageData(0, 0, 1, 1).data]; };
    const nav = document.querySelector(".nav-item.is-active"), surface = innerWidth <= 900 ? nav.querySelector(".nav-icon") : nav;
    await Promise.all(nav.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => {})));
    return { surface: rgba(getComputedStyle(surface).backgroundColor), text: rgba(getComputedStyle(element.closest(".studio-select-option")).color) };
  });
  await painted(mark, "backgroundColor", dropdownColors.surface, "dropdown selected checkbox must share the selected navigation's soft fill");
  await painted(mark, "borderTopColor", dropdownColors.text, "dropdown checkbox outline must use the option text color");
  await painted(mark, "color", dropdownColors.text, "dropdown checkbox tick must use the option text color");
  assert.ok(await tickContrast(mark) >= 4.5, "dropdown checkbox tick must remain readable");
  await page.keyboard.press("Escape");
  let attempts = 0;
  while (!await frame.locator(".gallery-card:has(.gallery-image-count)").count()) {
    assert.ok(attempts++ < 10, "isolated gallery lacks a multi-image group");
    const before = await frame.locator("#galleryPageLabel").textContent();
    await frame.locator("#galleryNext:not(:disabled)").click();
    await frame.waitForFunction((text) => document.getElementById("galleryPageLabel").textContent !== text, before);
  }
  await frame.locator(".gallery-card:has(.gallery-image-count) .gallery-info").first().click();
  await frame.locator('.detail-filmstrip-thumb[aria-current="true"]').waitFor();
}

async function detail(frame, expected) {
  const first = frame.locator('.detail-filmstrip-thumb[aria-current="true"]');
  const oldIndex = await first.getAttribute("data-detail-dot");
  await painted(first.locator(".detail-filmstrip-preview"), "borderTopColor", expected, "active filmstrip border changed source color");
  await rawShadow(first.locator(".detail-filmstrip-preview"), expected);
  const next = frame.locator(`.detail-filmstrip-thumb:not([data-detail-dot="${oldIndex}"])`).first();
  const neutralStyle = await next.locator(".detail-filmstrip-preview").evaluate((element) => ({ border: getComputedStyle(element).borderTopColor, shadow: getComputedStyle(element).boxShadow }));
  const nextIndex = await next.getAttribute("data-detail-dot");
  await next.click();
  await frame.locator(`.detail-filmstrip-thumb[data-detail-dot="${nextIndex}"][aria-current="true"]`).waitFor();
  const current = frame.locator(`.detail-filmstrip-thumb[data-detail-dot="${nextIndex}"] .detail-filmstrip-preview`);
  await painted(current, "borderTopColor", expected, "new filmstrip selection did not inherit source color");
  await rawShadow(current, expected);
  const former = frame.locator(`.detail-filmstrip-thumb[data-detail-dot="${oldIndex}"]`);
  assert.equal(await former.getAttribute("aria-current"), "false");
  const formerStyle = await former.locator(".detail-filmstrip-preview").evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => {})));
    return { border: getComputedStyle(element).borderTopColor, shadow: getComputedStyle(element).boxShadow };
  });
  assert.deepEqual(formerStyle, neutralStyle, "previous filmstrip did not return to its neutral border/shadow after switching");
  await frame.locator("#detailDelete").click();
  await frame.locator(".delete-image-choice").first().waitFor();
  const choice = frame.locator(".delete-image-choice").first();
  await painted(choice.locator("input"), "accentColor", expected, "native deletion checkbox accent changed source color");
  await painted(choice.locator("img"), "outlineColor", expected, "selected deletion preview outline changed source color");
  await choice.click();
  const unchecked = await choice.locator("img").evaluate((element) => ({ outline: getComputedStyle(element).outlineStyle, width: getComputedStyle(element).outlineWidth }));
  assert.ok(unchecked.outline === "none" || unchecked.width === "0px", "deselected deletion image retained outline");
  await frame.locator("#studioModalClose").click();
  await frame.locator("#closeDrawer").click();
}

async function mergePicker(frame, page, expected) {
  const image = await frame.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 160; canvas.height = 120;
    const context = canvas.getContext("2d"); context.fillStyle = "#527dab"; context.fillRect(0, 0, 160, 120); context.fillStyle = "#9cbfae"; context.fillRect(0, 70, 160, 50);
    return canvas.toDataURL("image/png");
  });
  await page.route("**/imports/merge-targets?*", (route) => route.fulfill({ json: { items: [{ id: "selection-color-target", model: "颜色测试图组", created_at: 1788690000, image_count: 2, prompt_preview: "测试选中外框", thumbnail_data_url: image }], total: 1, offset: 0, limit: 12 } }));
  await page.route("**/imports/check", (route) => route.fulfill({ json: { allowed: true, duplicate_hashes: [] } }));
  await frame.locator('[data-view="import"]').click();
  await frame.locator("#importFiles").setInputFiles({ name: "selection-colors.png", mimeType: "image/png", buffer: Buffer.from(image.split(",")[1], "base64") });
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  await frame.locator('.import-card [data-import-field="generation_engine"]').selectOption("novelai", { force: true });
  await frame.locator("#importMergeExisting").locator("xpath=..").click();
  await frame.locator("#confirmImportButton").click();
  const card = frame.locator('[data-merge-target="selection-color-target"]');
  await card.waitFor(); await card.click();
  await painted(card, "borderTopColor", expected, "merge target card border changed source color");
  await rawShadow(card, expected);
  await painted(card.locator('input[type="radio"]'), "backgroundColor", expected, "custom merge radio dot changed source color");
  await painted(card.locator('input[type="radio"]'), "borderTopColor", expected, "custom merge radio border changed source color");
  await frame.locator("#studioModalClose").click();
  await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  await frame.locator("#cancelImportButton").click();
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSERS || "chromium").split(",")) {
    const browser = await browsers[engine].launch({ headless: true });
    try {
      for (const width of [1440, 390]) for (const mode of ["light", "dark"]) {
        const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, hasTouch: width === 390, colorScheme: mode });
        const page = await context.newPage(); page.setDefaultTimeout(12000);
        const errors = [], mutations = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("request", (request) => { if (request.method() === "POST" && /\/(?:gallery\/.*delete|imports\/(?:prepare|upload|group))/.test(request.url())) mutations.push(request.url()); });
        await page.goto(base);
        const frame = page.frames().find((candidate) => candidate.url().includes("/ui/"));
        await frame.evaluate(() => window.ImageStudioAppearance.ready);
        try {
          for (const [name, settings] of [
            ["pale", { accentHue: 328.8888888888889, accentSaturation: 27.835051546391746, accentLightness: 80.98039215686275 }],
            ["vivid", { accentHue: 216, accentSaturation: 100, accentLightness: 50 }],
          ]) {
            const expected = await color(frame, mode, settings);
            if (name === "pale") assert.deepEqual(expected, [220, 193, 207, 255]);
            await gallery(frame, page, expected);
            await detail(frame, expected);
            await mergePicker(frame, page, expected);
            await page.screenshot({ path: path.join(output, `${engine}-${width}-${mode}-${name}.png`), animations: "disabled" });
          }
          assert.deepEqual(errors, []); assert.deepEqual(mutations, [], "selection style check must never upload or delete assets");
          console.log(`${engine} ${width}px ${mode}: soft dropdowns, raw gallery/filmstrip/merge/delete selections, readable ticks and no mutations passed`);
        } finally { await context.close(); }
      }
    } finally { await browser.close(); }
  }
  console.log(`Selection color screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
