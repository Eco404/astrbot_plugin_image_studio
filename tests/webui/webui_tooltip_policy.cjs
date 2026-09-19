/* Standalone tooltip policy checks: real controls and layout, no backend. */
const assert = require("node:assert/strict");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const assets = require("../support/webui_paths.cjs").frontend;

async function run(browser, engine, width) {
  const context = await browser.newContext({ viewport: { width, height: 950 }, hasTouch: width < 600 });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.setContent(`<!doctype html><html data-theme="light"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>
      <main id="fixture">
        <button id="start" type="button">起点</button>
        <label class="field">模型<select id="single"><option value="one">Alpha</option><option value="two">Beta</option></select></label>
        <label class="field">来源<select id="multi" multiple><option selected>A</option><option selected>B</option></select></label>
        <label class="field" id="longField">较长选项<select id="long"><option>generation-model-reference</option></select></label>
        <label class="field">额外说明<select id="explained" data-tooltip="切换模型会保留共有参数"><option>Alpha</option><option>Beta</option></select></label>
        <label class="field">暂不可用<select id="disabled" disabled data-tooltip="请先配置服务商"><option>未配置</option></select></label>
        <span id="filename" data-tooltip="reference.png">reference.png</span>
        <button id="command" type="button" data-tooltip="下载">下载</button>
        <button id="hiddenCommand" type="button" data-tooltip="下载" data-tooltip-overflow=".caption" aria-label="下载"><span aria-hidden="true">↓</span><span class="caption">下载</span></button>
        <button id="beforeHelp" type="button">前一项</button>
        <button id="help" type="button" data-tooltip="按原始大小下载图片">下载原图</button>
        <button id="afterHelp" type="button">后一项</button>
      </main>
    </body></html>`);
    for (const name of ["app.css", "library.css", "select.css", "tooltip.css"]) await page.addStyleTag({ path: path.join(assets, name) });
    await page.addStyleTag({ content: `body { margin:0; padding:24px; } #fixture { display:grid; gap:12px; max-width:300px; } #fixture > * { min-width:0; } #fixture > button { justify-self:start; } #filename { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } #hiddenCommand .caption { display:none; }` });
    for (const name of ["select.js", "tooltip.js"]) await page.addScriptTag({ path: path.join(assets, name) });
    const popup = page.locator("#studioTooltip");
    const trigger = id => page.locator(`.studio-select-trigger[data-select-id="${id}"]`);
    const clipped = locator => locator.evaluate(element => element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1);
    async function reset() {
      await page.evaluate(() => window.ImageStudioTooltip.hide(true));
      await page.mouse.move(2, 2);
    }
    async function absent(message) {
      // Greater than the ordinary hover delay: absence is not merely an early snapshot.
      await page.waitForTimeout(720);
      assert.equal(await popup.evaluateAll(elements => elements.some(element => !element.hidden && element.getAttribute("aria-hidden") === "false")), false, message);
    }
    async function hoverAbsent(locator, message) {
      await reset(); await locator.hover(); await absent(message);
    }
    async function shown(text) {
      await popup.waitFor({ state: "visible" });
      assert.equal(await popup.getAttribute("aria-hidden"), "false");
      assert.equal(await popup.textContent(), text);
    }
    async function hoverShown(locator, text) {
      await reset(); await locator.hover(); await shown(text);
    }

    await trigger("single").waitFor();
    await hoverAbsent(trigger("single"), "a readable single value does not repeat in a tooltip");
    await hoverAbsent(trigger("multi"), "the readable all-selected summary has no tooltip enumeration");
    await page.locator("#multi").evaluate(select => { select.options[1].selected = false; window.ImageStudioSelect.refresh(select); });
    await hoverAbsent(trigger("multi"), "a readable one-selected summary also remains quiet");
    await hoverShown(trigger("explained"), "切换模型会保留共有参数");
    await hoverShown(trigger("disabled"), "请先配置服务商");

    // Geometry is sampled when showing, including after a viewport/layout resize.
    await reset();
    const longValue = trigger("long").locator(".studio-select-value");
    assert.equal(await clipped(longValue), false, "fixture starts with a readable long value");
    await hoverAbsent(trigger("long"), "a wide value does not show a redundant tooltip");
    await page.setViewportSize({ width: 240, height: 950 });
    assert.equal(await clipped(longValue), true, "the actual select label must be clipped after resize");
    await hoverShown(trigger("long"), "generation-model-reference");
    await page.setViewportSize({ width, height: 950 });
    assert.equal(await clipped(longValue), false);
    await hoverAbsent(trigger("long"), "a widened select stops showing the old overflow hint");

    await hoverAbsent(page.locator("#filename"), "readable filenames are not repeated");
    await page.locator("#filename").evaluate(element => { element.style.width = "42px"; });
    assert.equal(await clipped(page.locator("#filename")), true);
    await hoverShown(page.locator("#filename"), "reference.png");
    await hoverAbsent(page.locator("#command"), "a fully readable text command has no duplicate hint");
    await hoverShown(page.locator("#hiddenCommand"), "下载");

    await reset();
    await page.locator("#beforeHelp").focus();
    await page.locator("#help").focus();
    await absent("programmatic focus does not show an ordinary hint");
    await page.locator("#beforeHelp").focus();
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#help").evaluate(element => document.activeElement === element), true);
    await shown("按原始大小下载图片");
    await page.keyboard.press("Escape");
    await absent("Escape closes a keyboard hint without reopening it");

    // Open real select menus from an explicit description; Enter / Escape return
    // focus to their trigger, which must not immediately display another popup.
    for (const key of ["Escape", "Enter"]) {
      await reset();
      await trigger("explained").focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await shown("切换模型会保留共有参数");
      await page.keyboard.press("Enter");
      await page.locator('.studio-select-menu[data-select-id="explained"]').waitFor({ state: "visible" });
      assert.equal(await popup.getAttribute("aria-hidden"), "true", "opening the menu hides its explanatory hint");
      if (key === "Enter") await page.keyboard.press("ArrowDown");
      await page.keyboard.press(key);
      await page.locator('.studio-select-menu[data-select-id="explained"]').waitFor({ state: "detached" });
      assert.equal(await trigger("explained").evaluate(element => document.activeElement === element), true);
      await absent(`${key} menu completion must not produce a focus-restoration hint`);
    }
    assert.deepEqual(errors, []);
    console.log(`${engine} ${width}px: duplicate suppression, overflow resize, explicit descriptions, disabled reasons and keyboard focus passed`);
  } finally { await context.close(); }
}

(async () => {
  for (const [engine, widths] of [["chromium", [1440, 390]], ["webkit", [390]]]) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of widths) await run(browser, engine, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
