/* Layout-only response fixtures; run against the isolated WebUI harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated tests.webui_harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-parameter-masonry-"));
const prompt = Array.from({ length: 18 }, (_, index) => `Landscape ${index + 1}: mountain lake, daylight and clear water.`).join("\n");

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  });
}

async function verifyGrid(grid, count) {
  const data = await grid.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return {
      height: rect.height, width: rect.width, gap: parseFloat(getComputedStyle(element).columnGap),
      rows: Array.from(element.children).map(row => {
        const bounds = row.getBoundingClientRect();
        return { key: row.querySelector(".detail-parameter-label span").textContent, x: bounds.left - rect.left, y: bounds.top - rect.top, width: bounds.width, height: bounds.height, margin: parseFloat(getComputedStyle(row).marginBottom) };
      }),
    };
  });
  const heights = [0, 0]; let column = 0;
  const columns = [];
  const width = (data.width - (count - 1) * data.gap) / count;
  for (const row of data.rows) {
    assert.ok(Math.abs(row.width - width) < 1.1, `${row.key}: column width ${JSON.stringify(data)}`);
    assert.ok(Math.abs(row.x - column * (width + data.gap)) < 1.1, `${row.key}: wrong column ${JSON.stringify(data)}`);
    assert.ok(Math.abs(row.y - heights[column]) < 1.1, `${row.key}: gap or overlap ${JSON.stringify(data)}`);
    heights[column] += Math.ceil(row.height + row.margin - 0.001);
    columns.push(column);
    if (count === 2 && heights[column] > heights[1 - column]) column = 1 - column;
  }
  assert.ok(Math.abs(data.height - Math.max(...heights)) < 1.1, "container must enclose both columns");
  return { ...data, columns };
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, hasTouch: true });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/, "isolated harness required");
    await page.route("**/gallery/detail/**", async route => {
      const response = await route.fetch();
      const detail = await response.json();
      detail.original_prompt = prompt;
      detail.parameters = { ...detail.parameters, negative_prompt: "blur", parameters: { steps: 24, sampler: "euler", scale: 6, long_note: prompt.slice(0, 500), seed: 42, enabled: false, guidance: 0.3 } };
      detail.images = detail.images.map(image => ({ ...image, metadata: {
        format: "comfyui", raw: {}, normalized: {
          prompt, steps: 24, sampler: "euler", seed: 42,
          stages: [{ node_id: "1", type: "KSampler", prompt, steps: 24, seed: 42, sampler: "euler", scale: 6, note: "stage note" }],
        },
      } }));
      await route.fulfill({ response, json: detail });
    });
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator(".detail-parameter-grid.is-masonry").first().waitFor();
    await page.waitForLoadState("networkidle");
    await settle(frame);
    const grid = frame.locator(".detail-parameter-grid").first();
    const desktop = await verifyGrid(grid, 2);
    const order = desktop.rows.map(row => row.key);
    assert.deepEqual(desktop.columns.slice(0, 4), [0, 1, 1, 1], "short fields must keep filling the right column below a tall prompt");
    await grid.evaluate(element => { document.getElementById("drawerBody").scrollTop += element.getBoundingClientRect().top - document.getElementById("drawerBody").getBoundingClientRect().top; });
    await page.screenshot({ path: path.join(output, `${engine}-desktop.png`) });

    await frame.evaluate(() => { window.__copied = []; Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => window.__copied.push(text) } }); });
    await grid.locator("[data-copy-field]").first().click();
    assert.equal(await frame.evaluate(() => window.__copied[0]), prompt, "copy values retain original field mapping");
    await grid.evaluate(element => Array.from(element.children).forEach(row => { row.style.height = "50px"; row.style.overflow = "hidden"; }));
    await settle(frame);
    assert.deepEqual((await verifyGrid(grid, 2)).columns.slice(0, 5), [0, 1, 1, 0, 0], "equal heights keep the current column until it exceeds the other");
    await grid.evaluate(element => Array.from(element.children).forEach(row => { row.style.removeProperty("height"); row.style.removeProperty("overflow"); }));
    await settle(frame);
    await verifyGrid(grid, 2);

    const stage = frame.locator("#drawerBody .comfy-stage").first();
    await frame.locator("#drawerBody .comfy-workflow-info > summary").click();
    await stage.locator(":scope > summary").click();
    await settle(frame);
    await verifyGrid(stage.locator(".detail-parameter-grid"), 2);
    await stage.locator(":scope > summary").click();
    await stage.locator(":scope > summary").click();
    await settle(frame);
    await verifyGrid(stage.locator(".detail-parameter-grid"), 2);

    for (const width of [900, 390, 1440]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await settle(frame);
      const layout = await verifyGrid(grid, width === 390 ? 1 : 2);
      assert.deepEqual(layout.rows.map(row => row.key), order, "resizing does not reorder the DOM or keyboard navigation");
      await verifyGrid(stage.locator(".detail-parameter-grid"), width === 390 ? 1 : 2);
      if (width === 390) {
        await grid.evaluate(element => { document.getElementById("drawerBody").scrollTop += element.getBoundingClientRect().top - document.getElementById("drawerBody").getBoundingClientRect().top; });
        await page.screenshot({ path: path.join(output, `${engine}-mobile.png`) });
      }
    }

    await frame.locator('[data-detail-nav="1"]').evaluate(button => button.click());
    await page.waitForLoadState("networkidle");
    await settle(frame);
    await verifyGrid(frame.locator(".detail-parameter-grid").first(), 2);
    await frame.locator("#closeDrawer").click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await page.waitForLoadState("networkidle");
    await settle(frame);
    await verifyGrid(frame.locator(".detail-parameter-grid").first(), 2);
    assert.deepEqual(errors, [], "no runtime or ResizeObserver loop errors");
    console.log(`${engine}: height-driven columns, equal-height ties, resizing, nested stages, copying and navigation passed`);
    console.log(`Screenshots: ${output}`);
    await page.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
