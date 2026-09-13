/* Standalone select contract and scrolling regression; no runtime data or server needed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");

const root = path.resolve(__dirname, "../pages/image-studio");
const key = "image_studio.gallery.filter_defaults.v1";
const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/select.css"><link rel="stylesheet" href="/appearance.css">
<style>:root { --line: #73847844; --surface: #ffffff44; --surface-hover: #ffffff66; --text: #263c31; --muted: #607268; }
* { box-sizing: border-box; } body { margin: 20px; min-height: 2000px; font: 14px system-ui; background: repeating-linear-gradient(30deg, #c8dcce 0px 60px, #dad8ee 60px 120px); }
label { display: block; width: 240px; max-width: 100%; margin-bottom: 16px; } #glassProbe { background: var(--glass); }</style>
<script src="/appearance.js" defer></script><script src="/gallery-preferences.js" defer></script><script src="/select.js" defer></script></head><body>
<label>服务商<select id="galleryProvider" multiple data-all-label="全部服务商">
${Array.from({ length: 30 }, (_, index) => `<option value="${index ? `provider-${index}` : ""}" selected>${index ? `服务商 ${index}` : "未指定"}</option>`).join("")}
</select></label>
<label>模式<select id="galleryMode" multiple><option value="text2img" selected>文生图</option><option value="img2img" selected>图生图</option></select></label>
<label>其他多选<select id="unrelated" multiple><option selected>A</option><option selected>B</option></select></label>
<label>普通选择<select id="single"><option>第一项</option><option>第二项</option></select></label>
<label>输入模型<input id="editable" list="models"><datalist id="models"><option value="model-a">模型 A</option><option value="model-b">模型 B</option></datalist></label>
<div id="glassProbe"></div></body></html>`;

async function prepare(page) {
  await page.route("http://image-studio-select.test/**", (route) => {
    const name = new URL(route.request().url()).pathname.slice(1);
    if (["gallery-preferences.js", "select.js", "select.css", "appearance.css", "appearance.js"].includes(name)) return route.fulfill({ contentType: name.endsWith("js") ? "application/javascript" : "text/css", body: fs.readFileSync(path.join(root, name), "utf8") });
    return route.fulfill({ contentType: "text/html", body: html });
  });
  await page.goto("http://image-studio-select.test/");
  await page.locator('[data-select-id="galleryProvider"].studio-select-trigger').waitFor();
}

const trigger = (page, id) => page.locator(`.studio-select-trigger[data-select-id="${id}"]`);
const action = (page, name) => page.locator(`.studio-select-menu [data-select-action="${name}"]`);
const saved = (page, id) => page.evaluate((id) => window.ImageStudioSelect.getGalleryDefault(id), id);
async function chooseIndex(page, index) { await page.locator(`.studio-select-menu [data-option-index="${index}"]`).click(); }
async function frames(page) { await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); }

async function matchingMaterials(page) {
  async function snapshot(id) {
    await trigger(page, id).click();
    const result = await page.locator(".studio-select-menu").evaluate(async (menu) => {
      await Promise.all(menu.getAnimations().map((animation) => animation.finished.catch(() => {})));
      const surface = getComputedStyle(menu);
      const row = menu.querySelector('[aria-selected="true"]');
      const selected = getComputedStyle(row), mark = getComputedStyle(row.querySelector(".studio-select-mark"));
      const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d");
      const alpha = (color) => { context.clearRect(0, 0, 1, 1); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return context.getImageData(0, 0, 1, 1).data[3] / 255; };
      return {
        surface: { background: surface.backgroundColor, color: surface.color, border: surface.borderTopColor, radius: surface.borderRadius, shadow: surface.boxShadow, blur: surface.backdropFilter || surface.webkitBackdropFilter },
        selected: { background: selected.backgroundColor, color: selected.color, shadow: selected.boxShadow, alpha: alpha(selected.backgroundColor), textAlpha: alpha(selected.color), opacity: selected.opacity, blur: selected.backdropFilter || selected.webkitBackdropFilter },
        mark: { background: mark.backgroundColor, alpha: alpha(mark.backgroundColor), color: mark.color, textAlpha: alpha(mark.color), opacity: mark.opacity, border: mark.borderTopColor, width: mark.borderTopWidth, radius: mark.borderRadius },
      };
    });
    await trigger(page, id).press("Escape");
    return result;
  }
  for (const theme of ["light", "dark"]) for (const opacity of [0.2, 0.68, 1]) {
    await page.evaluate(({ theme, opacity }) => {
      window.ImageStudioAppearance.set({ preference: theme, glassOpacity: opacity, accentHue: 345 });
    }, { theme, opacity });
    const single = await snapshot("single");
    assert.ok(single.selected.alpha > .5 && single.selected.alpha < .85, "selected row must reveal the existing menu glass rather than paint an opaque surface");
    assert.equal(single.selected.textAlpha, 1, "selection must not fade the option text");
    assert.equal(single.selected.opacity, "1", "selection transparency belongs to the background, not the entire row");
    assert.ok(!single.selected.blur || single.selected.blur === "none", "selected rows should reuse menu blur without another backdrop filter");
    for (const id of ["galleryProvider", "unrelated"]) {
      const multiple = await snapshot(id);
      assert.deepEqual(multiple.surface, single.surface, `${theme}/${opacity}/${id}: multiple and single menus must use the same glass material`);
      assert.deepEqual(multiple.selected, single.selected, `${theme}/${opacity}/${id}: selected row palette must match single selects`);
      assert.equal(multiple.mark.color, single.mark.color, "single and multiple checkmarks use the same text color");
      assert.equal(multiple.mark.alpha, 0, "selected checkbox must not paint a second translucent color layer");
      assert.equal(multiple.mark.textAlpha, 1, "selected checkbox tick remains opaque");
      assert.equal(multiple.mark.opacity, "1", "selected checkbox must not fade its tick or border");
      assert.equal(multiple.mark.border, multiple.selected.color, "checkbox outline remains discernible against the soft fill");
      assert.ok(parseFloat(multiple.mark.width) > 0 && parseFloat(multiple.mark.radius) < 9, "multiple selection keeps its small checkbox outline");
    }
  }
  await page.evaluate(() => window.ImageStudioAppearance.set({ preference: "light", glassOpacity: .68 }));
}

async function test(browserType, name, viewport) {
  const browser = await browserType.launch({ headless: true });
  const page = await browser.newPage({ viewport, hasTouch: viewport.width < 600 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await prepare(page);
    await matchingMaterials(page);
    assert.equal(await saved(page, "galleryProvider"), null);
    await trigger(page, "galleryProvider").click();
    const geometry = await page.locator(".studio-select-menu").evaluate((menu) => {
      const list = menu.querySelector(".studio-select-options"); const bar = menu.querySelector(".studio-select-actions");
      const box = menu.getBoundingClientRect(); const before = bar.getBoundingClientRect(); list.scrollTop = 720;
      const after = bar.getBoundingClientRect(); const listBox = list.getBoundingClientRect();
      const hit = document.elementFromPoint(before.left + 1, before.top + before.height / 2);
      const css = getComputedStyle(menu);
      const alpha = color => { const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1; const context = canvas.getContext("2d"); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return context.getImageData(0, 0, 1, 1).data[3] / 255; };
      return { before: before.y, after: after.y, listTop: listBox.top, barBottom: after.bottom, scroll: list.scrollTop, canScroll: list.scrollHeight > list.clientHeight, behind: !!hit?.closest(".studio-select-option"), overflow: css.overflowY, glass: css.backgroundColor, alpha: alpha(css.backgroundColor), themeAlpha: alpha(getComputedStyle(document.getElementById("glassProbe")).backgroundColor), blur: css.backdropFilter || css.webkitBackdropFilter, left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: innerWidth, height: innerHeight };
    });
    assert.equal(geometry.before, geometry.after);
    assert.ok(geometry.listTop >= geometry.barBottom - 1, JSON.stringify(geometry));
    assert.ok(geometry.scroll > 0 && geometry.canScroll, JSON.stringify(geometry));
    assert.equal(geometry.behind, false);
    assert.equal(geometry.overflow, "hidden");
    assert.ok(Math.abs(geometry.alpha - geometry.themeAlpha) < .01, "gallery filter must not add a second opacity layer over the shared menu glass");
    assert.match(geometry.blur, /blur\(22px\)/);
    assert.ok(geometry.left >= 7 && geometry.top >= 7 && geometry.right <= geometry.width - 7 && geometry.bottom <= geometry.height - 7, JSON.stringify(geometry));
    const wheel = await page.locator(".studio-select-menu").evaluate((menu) => {
      const list = menu.querySelector(".studio-select-options");
      const dispatch = (target, delta) => { const event = new WheelEvent("wheel", { deltaY: delta, bubbles: true, cancelable: true }); target.dispatchEvent(event); return event.defaultPrevented; };
      const middle = dispatch(list, 10); list.scrollTop = 0;
      return { middle, top: dispatch(list, -10), bar: dispatch(menu.querySelector(".studio-select-actions"), 10) };
    });
    assert.deepEqual(wheel, { middle: false, top: true, bar: true });
    await trigger(page, "galleryProvider").press("End");
    assert.equal(await page.locator('.studio-select-option.is-active').getAttribute("data-option-index"), "29");
    const activeVisible = await page.locator(".studio-select-options").evaluate((list) => { const row = list.querySelector(".is-active").getBoundingClientRect(); const bounds = list.getBoundingClientRect(); return row.top >= bounds.top - 1 && row.bottom <= bounds.bottom + 1; });
    assert.equal(activeVisible, true);
    await action(page, "clear").click();
    assert.equal(await action(page, "default").isDisabled(), true);
    assert.equal(await saved(page, "galleryProvider"), null, "changing selection must not save automatically");
    await trigger(page, "galleryProvider").press("Home");
    await chooseIndex(page, 0);
    await chooseIndex(page, 2);
    await action(page, "default").click();
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "values", values: ["", "provider-2"] }, "the unspecified provider is a real selectable value");
    await page.evaluate(() => { window.ImageStudioSelect.getGalleryDefault("galleryProvider").values.push("mutation"); });
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "values", values: ["", "provider-2"] }, "callers must not mutate stored preferences");
    await page.reload();
    await trigger(page, "galleryProvider").waitFor();
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "values", values: ["", "provider-2"] }, "subset survives page reload");
    await trigger(page, "galleryProvider").click();
    await action(page, "default").click();
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "all" });
    await page.evaluate(() => { document.getElementById("galleryProvider").add(new Option("新服务商", "new-provider")); window.ImageStudioSelect.refresh(); });
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "all" }, "an all default must not freeze the current option list");
    await trigger(page, "galleryProvider").press("Escape");
    await trigger(page, "galleryMode").click();
    await action(page, "clear").click();
    await chooseIndex(page, 1);
    await trigger(page, "galleryMode").press("Control+Enter");
    assert.deepEqual(await saved(page, "galleryMode"), { mode: "values", values: ["img2img"] });
    assert.deepEqual(await saved(page, "galleryProvider"), { mode: "all" }, "saving another dropdown must preserve its sibling defaults");
    await trigger(page, "galleryMode").press("Escape");
    await trigger(page, "unrelated").click();
    assert.equal(await page.locator(".studio-select-menu").evaluate(menu => getComputedStyle(menu).backgroundColor), await page.locator("#glassProbe").evaluate(probe => getComputedStyle(probe).backgroundColor), "other selects keep their theme opacity");
    assert.equal(await action(page, "default").count(), 0, "save-default belongs only to the gallery filters");
    await trigger(page, "unrelated").press("Escape");
    await trigger(page, "single").click();
    assert.equal(await page.locator(".studio-select-actions").count(), 0);
    await chooseIndex(page, 1);
    assert.equal(await page.locator("#single").inputValue(), "第二项");
    await page.locator("#editable").fill("model");
    await page.locator(".studio-select-menu").waitFor();
    await page.locator("#editable").press("ArrowDown");
    await page.locator("#editable").press("Enter");
    assert.equal(await page.locator("#editable").inputValue(), "model-a");
    for (const invalid of ["not json", "[]", '{"galleryProvider":{"mode":"values","values":[]}}', '{"galleryProvider":{"mode":"values","values":[null]}}']) {
      await page.evaluate(({ key, invalid }) => localStorage.setItem(key, invalid), { key, invalid });
      await page.reload(); await trigger(page, "galleryProvider").waitFor();
      assert.equal(await saved(page, "galleryProvider"), null);
    }
    await page.evaluate(() => {
      window.__defaultError = null;
      window.addEventListener("image-studio-gallery-default-error", (event) => { window.__defaultError = event.detail; });
      Object.defineProperty(window, "localStorage", { configurable: true, get() { throw new DOMException("Storage blocked", "SecurityError"); } });
    });
    assert.equal(await saved(page, "galleryProvider"), null);
    await trigger(page, "galleryProvider").click();
    await action(page, "default").click();
    assert.match(await page.evaluate(() => window.__defaultError?.message), /无法保存/);
    await frames(page);
    assert.deepEqual(errors, []);
    console.log(`${name}: shared translucent single/multiple selection, opaque text, no repeated checkbox fill, glass theme, separate scrolling, keyboard/datalist and saved defaults passed`);
  } finally { await browser.close(); }
}

(async () => {
  for (const [browser, label] of [[chromium, "Chromium"], [webkit, "WebKit"]]) {
    await test(browser, `${label} desktop`, { width: 1280, height: 900 });
    await test(browser, `${label} mobile`, { width: 390, height: 844 });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
