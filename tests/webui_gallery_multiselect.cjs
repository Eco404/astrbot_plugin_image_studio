/* Real gallery API, isolated harness data only. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-gallery-multiselect-"));
const keys = { galleryProvider: "provider_ids", galleryMode: "modes", gallerySource: "sources", galleryEngine: "generation_engines" };

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    const initial = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=60`)).json();
    assert.ok(initial.total > 24);
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card").first().waitFor();
    const selected = id => frame.locator(`#${id}`).evaluate(select => Array.from(select.selectedOptions, option => option.value));
    const trigger = id => frame.locator(`.studio-select-trigger[data-select-id="${id}"]`);
    const menu = id => frame.locator(`.studio-select-menu[data-select-id="${id}"]`);
    for (const id of Object.keys(keys)) {
      const defaults = await frame.locator(`#${id}`).evaluate(select => ({ multiple: select.multiple, all: [...select.options].every(option => option.selected), label: select.dataset.allLabel }));
      assert.ok(defaults.multiple && defaults.all, `${id} must default to all selected`);
      assert.equal(await trigger(id).locator(".studio-select-value").textContent(), defaults.label);
    }

    async function change(id, action, values) {
      if (await trigger(id).getAttribute("aria-expanded") !== "true") await trigger(id).click();
      const response = page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname.endsWith("/gallery/list") && url.searchParams.get(keys[id]) === (values === null ? null : JSON.stringify(values));
      });
      await action(menu(id));
      const result = await response;
      assert.ok(result.ok(), await result.text());
      const payload = await result.json();
      await frame.locator("#galleryGrid").evaluate(async () => { await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); });
      assert.equal(await trigger(id).getAttribute("aria-expanded"), "true", "checkbox selection must not close menu");
      assert.equal(new URL(result.url()).searchParams.get("offset"), "0", "filter changes restart pagination");
      if (values !== null) assert.deepEqual(await selected(id), values);
      return payload;
    }
    async function toggle(id, value, values) {
      const index = await frame.locator(`#${id}`).evaluate((select, desired) => [...select.options].findIndex(option => option.value === desired), value);
      assert.ok(index >= 0);
      return change(id, menu => menu.locator(`[data-option-index="${index}"]`).click(), values);
    }
    async function close(id) { await trigger(id).press("Escape"); }

    await frame.locator("#galleryNext").click();
    await frame.locator("#galleryPageLabel").filter({ hasText: "第 2" }).waitFor();
    let payload = await toggle("gallerySource", "webui", ["command", "llm_tool", "import"]);
    assert.equal(payload.total, initial.items.filter(item => item.source !== "webui").length);
    await toggle("gallerySource", "import", ["command", "llm_tool"]);
    await page.screenshot({ path: path.join(output, `${name}-sources.png`) });
    await close("gallerySource");

    payload = await toggle("galleryProvider", "", ["nai", "natural"]);
    assert.equal(payload.total, initial.items.filter(item => item.source !== "webui").length);
    payload = await toggle("galleryProvider", "nai", ["natural"]);
    const expected = initial.items.filter(item => item.source !== "webui" && item.provider_id === "natural");
    assert.equal(payload.total, expected.length);
    assert.deepEqual(payload.items.map(item => item.id), expected.map(item => item.id));
    await close("galleryProvider");
    const refresh = page.waitForResponse(response => response.url().includes("/gallery/list"));
    await frame.locator("#galleryRefresh").click(); await refresh;
    assert.deepEqual(await selected("galleryProvider"), ["natural"]);
    assert.deepEqual(await selected("gallerySource"), ["command", "llm_tool"]);

    const sequenceResponse = page.waitForResponse(response => response.url().includes("/gallery/image-sequence"));
    await frame.locator(".gallery-card .gallery-info").first().click();
    const sequence = await sequenceResponse;
    const sequenceParams = new URL(sequence.url()).searchParams;
    assert.equal(sequenceParams.get("sources"), '["command","llm_tool"]');
    assert.equal(sequenceParams.get("provider_ids"), '["natural"]');
    const sequenceItems = (await sequence.json()).items;
    assert.deepEqual([...new Set(sequenceItems.map(item => item.generation_id))], expected.map(item => item.id));
    await frame.locator("#closeDrawer").click();

    payload = await change("galleryMode", menu => menu.locator('[data-select-action="clear"]').click(), []);
    assert.equal(payload.total, 0);
    await frame.locator("#galleryEmpty").filter({ hasText: "没有符合当前筛选条件" }).waitFor();
    await page.screenshot({ path: path.join(output, `${name}-none.png`) });
    payload = await toggle("galleryMode", "text2img", ["text2img"]);
    assert.equal(payload.total, expected.length);
    await change("galleryMode", menu => menu.locator('[data-select-action="all"]').click(), null);
    await close("galleryMode");

    await change("galleryEngine", menu => menu.locator('[data-select-action="clear"]').click(), []);
    payload = await toggle("galleryEngine", "openai_images", ["openai_images"]);
    assert.equal(payload.total, expected.length);
    payload = await toggle("galleryEngine", "novelai", ["novelai", "openai_images"]);
    assert.equal(payload.total, expected.length);
    await change("galleryEngine", menu => menu.locator('[data-select-action="all"]').click(), null);
    await close("galleryEngine");
    await change("galleryProvider", menu => menu.locator('[data-select-action="all"]').click(), null);
    await close("galleryProvider");
    payload = await change("gallerySource", menu => menu.locator('[data-select-action="all"]').click(), null);
    assert.equal(payload.total, initial.total);
    await close("gallerySource");

    // Newly discovered provider/engine options inherit all, but never a subset.
    await page.route("**/gallery/list?*", async route => {
      const response = await route.fetch(); const body = await response.json();
      body.filters.providers.push({ id: "fresh-provider", name: "新增服务商" });
      body.filters.generation_engines.push("mixed");
      await route.fulfill({ response, json: body });
    });
    const newOptions = page.waitForResponse(response => response.url().includes("/gallery/list"));
    await frame.locator("#galleryRefresh").click(); await newOptions;
    await frame.locator('#galleryProvider option[value="fresh-provider"]').waitFor({ state: "attached" });
    assert.ok((await selected("galleryProvider")).includes("fresh-provider"));
    assert.ok((await selected("galleryEngine")).includes("mixed"));
    await toggle("galleryProvider", "fresh-provider", ["", "nai", "natural"]);
    await close("galleryProvider");
    const again = page.waitForResponse(response => response.url().includes("/gallery/list"));
    await frame.locator("#galleryRefresh").click(); await again;
    assert.deepEqual(await selected("galleryProvider"), ["", "nai", "natural"]);
    await page.unroute("**/gallery/list?*");
    const inner = page.frames().find(frame => frame.url().includes("/ui/"));
    assert.ok(await inner.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
    assert.deepEqual(errors, []);
    console.log(`${name}: default all, multi-value intersections, empty selection, pagination, detail sequence and dynamic option preservation passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, type] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await type.launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, `${name}-${width}`, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
