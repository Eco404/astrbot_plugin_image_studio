/* Standalone sorting mechanics with safe synthetic cards and no backend. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const assets = require("../support/webui_paths.cjs").frontend;

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: 800 }, hasTouch: width < 600 });
  page.setDefaultTimeout(6000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.setContent(`<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>
      <style>:root{--accent:#409e91;--topbar-glass:#fff;--surface:#eef4f2}*{box-sizing:border-box}body{margin:0;padding:16px;font:14px sans-serif}.scroller{height:440px;overflow:auto;border:1px solid #ccc;padding:8px}#grid{display:grid;grid-template-columns:repeat(${width < 600 ? 1 : 3},minmax(0,1fr));gap:12px}.card{height:170px;background:#eef4f2;padding:8px}.import-card-header{display:flex;align-items:center;gap:6px}.import-card-preview{height:60px;background:#ccd6d3;margin:4px 0}button{width:42px;height:42px}input{width:100%}</style>
      <div class="scroller" id="scroller"><div id="grid">${["a", "b", "c", "d", "e", "f"].map(id => `<article class="card" data-import-id="${id}"><div class="import-card-header"><button data-sort-handle aria-label="排序 ${id}">↕</button><strong>${id}.png</strong></div><div class="import-card-preview" data-sort-surface></div><input value="prompt-${id}" /></article>`).join("")}</div></div><button id="outside">Outside</button></body></html>`);
    await page.addStyleTag({ path: path.join(assets, "sortable.css") });
    await page.addScriptTag({ path: path.join(assets, "sortable.js") });
    await page.evaluate(() => {
      window.sortCalls = []; window.sortEnabled = true;
      window.sorter = window.ImageStudioSortable.bind(document.getElementById("grid"), { isEnabled: () => window.sortEnabled, onReorder: ids => window.sortCalls.push(ids) });
    });
    const card = id => page.locator(`[data-import-id="${id}"]`);
    const handle = id => card(id).locator("[data-sort-handle]");
    const order = () => page.locator("#grid > [data-import-id]").evaluateAll(nodes => nodes.map(node => node.dataset.importId));
    async function moveTo(id, target, surface = false) {
      const from = await (surface ? card(id).locator("[data-sort-surface]") : handle(id)).boundingBox();
      const to = await handle(target).boundingBox();
      await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
      await page.mouse.down();
      await page.mouse.move(to.x + to.width / 2 + 16, to.y + to.height / 2, { steps: 6 });
      await page.locator(".studio-sort-ghost").waitFor();
      return { from, to };
    }
    await card("a").locator("input").fill("custom notes survive sorting");
    await moveTo("a", "b");
    assert.deepEqual(await order(), ["a", "b", "c", "d", "e", "f"], "draft DOM stays stable until drop");
    await page.mouse.up();
    assert.deepEqual(await order(), ["b", "a", "c", "d", "e", "f"]);
    assert.equal(await card("a").locator("input").inputValue(), "custom notes survive sorting");
    assert.equal(await page.locator(".studio-sort-ghost").count(), 0);
    await handle("a").press("ArrowUp");
    assert.deepEqual(await order(), ["a", "b", "c", "d", "e", "f"]);
    await handle("a").press("End");
    assert.deepEqual(await order(), ["b", "c", "d", "e", "f", "a"]);
    await handle("a").press("Home");
    assert.deepEqual(await order(), ["a", "b", "c", "d", "e", "f"]);
    await page.locator("#scroller").evaluate(element => { element.scrollTop = 0; });
    await moveTo("a", "b");
    await page.keyboard.press("Escape"); await page.mouse.up();
    assert.deepEqual(await order(), ["a", "b", "c", "d", "e", "f"]);
    assert.equal(await page.locator(".studio-sort-ghost").count(), 0);
    await moveTo("a", "b");
    await page.evaluate(() => { window.sortEnabled = false; });
    await page.mouse.up();
    assert.deepEqual(await order(), ["a", "b", "c", "d", "e", "f"], "busy editor cannot commit a pending sort");
    await page.evaluate(() => { window.sortEnabled = true; });
    await moveTo("a", "b", true); await page.mouse.up();
    assert.deepEqual(await order(), ["b", "a", "c", "d", "e", "f"], "desktop preview can also be dragged");
    await handle("a").press("Home");
    await card("a").locator("[data-sort-surface]").evaluate(original => {
      const button = document.createElement("button");
      button.type = "button"; button.className = original.className;
      button.dataset.sortSurface = ""; button.textContent = "选择条目 A";
      original.replaceWith(button);
    });
    const surface = card("a").locator("[data-sort-surface]");
    for (const selected of ["true", "false", null]) {
      await surface.evaluate((button, value) => value === null ? button.removeAttribute("aria-pressed") : button.setAttribute("aria-pressed", value), selected);
      await moveTo("a", "b", true); await page.mouse.up();
      assert.equal(await surface.getAttribute("aria-pressed"), selected, "surface dragging preserves the existing selection attribute");
      await handle("a").press("Home");
    }
    await surface.evaluate(button => button.setAttribute("aria-pressed", "true"));
    await moveTo("a", "b", true); await page.keyboard.press("Escape"); await page.mouse.up();
    assert.equal(await surface.getAttribute("aria-pressed"), "true", "canceling a surface drag also restores selection");
    await page.locator("#scroller").evaluate(element => { element.scrollTop = 0; });
    if (name === "chromium" && width < 600) {
      const session = await page.context().newCDPSession(page);
      const from = await handle("a").boundingBox(); const to = await handle("b").boundingBox();
      const touch = (x, y) => [{ x, y }];
      await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: touch(from.x + 20, from.y + 20) });
      await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: touch(to.x + 20, to.y + 20) });
      await page.locator(".studio-sort-ghost").waitFor();
      await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      assert.deepEqual(await order(), ["b", "a", "c", "d", "e", "f"], "touch handle drag must commit");
      await session.detach();
      await handle("a").press("Home");
    }
    await page.locator("#scroller").evaluate(element => { element.scrollTop = 0; });
    const start = await handle("a").boundingBox();
    const bounds = await page.locator("#scroller").boundingBox();
    await page.mouse.move(start.x + 20, start.y + 20); await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height - 12, { steps: 5 });
    if (width < 600) await page.waitForFunction(() => document.getElementById("scroller").scrollTop > 40);
    assert.equal(await page.evaluate(() => window.scrollY), 0, "editor drag scrolling stays inside its scroll container");
    await page.evaluate(() => window.sorter.cancel()); await page.mouse.up();
    await page.evaluate(() => window.sorter.destroy());
    assert.equal(await page.locator(".studio-sort-status").count(), 0);
    assert.equal(await page.locator(".is-sorting,.is-sort-dragging,.studio-sort-ghost").count(), 0);
    assert.deepEqual(errors, []);
    console.log(`${name}-${width}: pointer/keyboard sorting, cancellation, preserved inputs and contained scrolling passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await engine.launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, name, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
