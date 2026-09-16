/* Legacy gallery response fixtures; no provider requests or persisted edits. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-detail-request-fields-"));
const graph = { "1": { class_type: "CustomNode", inputs: { text: "", optional: null, enabled: false, seed: 0, _comfy_job_id: "node-owned-value" } } };
const parameters = {
  empty_text: "", whitespace: " \n\t ", missing: null, empty_array: [], empty_object: {},
  seed: 0, enabled: false, text_zero: "0", custom_parameter: "user value", _comfy_custom: "user-defined field",
  nested: { prompt: "", values: [], optional: null }, values: ["", null, {}], workflow: graph, _comfy_job_id: "internal-task-id",
};
const request = { negative_prompt: "", size: "", count: 1, parameters };

async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 950 }, hasTouch: width < 600 });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    let factory;
    Object.defineProperty(window, "ImageStudioLibrary", { configurable: true, get: () => factory, set(value) {
      factory = hooks => { window.__requestState = hooks.state; window.__requestHooks = hooks; return value(hooks); };
    } });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async content => { window.__copiedField = content; } } });
  });
  const response = await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=24`);
  assert.ok(response.ok());
  const cards = (await response.json()).items.slice(0, 3);
  assert.equal(cards.length, 3, "isolated harness supplies gallery records");
  const specs = new Map(cards.map((card, index) => [card.id, { effective: index !== 1, kind: index === 2 ? "openai_images" : "comfyui" }]));
  function fixture(detail, image, spec) {
    Object.assign(detail, { source: "webui", provider_kind: spec.kind, generation_engine: spec.kind, original_prompt: " \t ", parameters: structuredClone(request) });
    if (!image) return;
    image.supplemental = spec.effective ? { effective_request: structuredClone(request) } : {};
    image.metadata = { format: spec.kind === "comfyui" ? "comfyui" : "unknown", normalized: { empty_metadata: "", nested: { empty: null } }, raw: { prompt: JSON.stringify(graph), empty_raw: "" }, warnings: [] };
  }
  await page.route("**/gallery/detail/**", async route => {
    const response = await route.fetch(), detail = await response.json(), spec = specs.get(detail.id);
    if (spec) { fixture(detail, null, spec); detail.images.forEach(image => fixture(detail, image, spec)); }
    await route.fulfill({ response, json: detail });
  });
  await page.route("**/gallery/image-info/**", async route => {
    const response = await route.fetch(), payload = await response.json(), spec = specs.get(payload.detail_fields.id || payload.image.generation_id);
    if (spec) fixture(payload.detail_fields, payload.image, spec);
    await route.fulfill({ response, json: payload });
  });
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    for (const [id, spec] of specs) {
      await frame.locator(`[data-gallery-id="${id}"] .gallery-info`).click();
      await frame.locator(".detail-parameter-grid.is-masonry").first().waitFor();
      await frame.waitForFunction(() => !document.getElementById("detailFooter").inert);
      const block = frame.locator("#drawerBody > .detail-block").first();
      assert.equal(await block.locator("h3").textContent(), spec.effective ? "实际请求" : "原始请求");
      const rows = await block.locator(".detail-parameter-row").evaluateAll(items => Object.fromEntries(items.map(row => [row.querySelector(".detail-parameter-label span").textContent, row.querySelector("pre").textContent])));
      for (const key of ["prompt", "negative_prompt", "size", "empty_text", "whitespace", "missing", "empty_array", "empty_object", "_comfy_job_id"]) assert.ok(!(key in rows), `${spec.kind}: legacy empty/internal field ${key} must not leave a request box`);
      assert.ok(Object.values(rows).every(value => value.trim()), "request rows have no blank content");
      assert.equal(rows.seed, "0"); assert.equal(rows.enabled, "false"); assert.equal(rows.text_zero, "0");
      assert.equal(rows.custom_parameter, "user value"); assert.equal(rows._comfy_custom, "user-defined field");
      for (const key of ["nested", "values", "workflow"]) assert.deepEqual(JSON.parse(rows[key]), parameters[key], "complex parameters retain all nested empty values");
      const workflowRow = block.locator(".detail-parameter-row").filter({ has: frame.locator('.detail-parameter-label > span', { hasText: /^workflow$/ }) });
      await workflowRow.locator("[data-copy-field]").click();
      await frame.waitForFunction(() => window.__copiedField !== undefined);
      assert.deepEqual(JSON.parse(await frame.evaluate(() => window.__copiedField)), graph, "per-field copy preserves workflow contents");
      const preserved = await frame.evaluate(() => {
        const detail = window.__requestState.detailData;
        return { stored: detail.parameters, replay: window.__requestHooks.requestParameters(detail), effective: detail.images[window.__requestState.detailImageIndex].supplemental.effective_request };
      });
      assert.deepEqual(preserved.stored, request, "rendering does not mutate legacy stored values");
      assert.equal(preserved.replay.negative_prompt, "", "an explicitly empty negative prompt remains available for reproduction");
      assert.deepEqual(preserved.replay.parameters, parameters);
      if (spec.effective) assert.deepEqual(preserved.effective, request);
      const generated = frame.locator("#drawerBody .generated-parameters");
      await generated.locator(":scope > summary").click();
      assert.equal(await generated.locator(".detail-parameter-row").first().locator("pre").textContent(), "", "metadata display is outside request filtering");
      const raw = frame.locator("#drawerBody .raw-metadata");
      await raw.locator(":scope > summary").click();
      const rawPrompt = raw.locator(".metadata-raw-field").filter({ has: frame.locator("summary", { hasText: /^prompt$/ }) });
      await rawPrompt.locator(":scope > summary").click();
      assert.equal(await rawPrompt.locator("pre").textContent(), JSON.stringify(graph), "raw metadata JSON is unchanged");
      await block.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `${width}-${spec.kind}-${spec.effective ? "actual" : "original"}.png`) });
      await frame.locator("#closeDrawer").click();
      await frame.waitForFunction(() => !document.getElementById("detailDrawer").classList.contains("is-open"));
    }
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
}

(async () => {
  const browser = await playwright[process.env.STUDIO_BROWSER || "chromium"].launch({ headless: true });
  try { for (const width of [1440, 390]) await verify(browser, width); }
  finally { await browser.close(); }
  console.log(`Legacy request display passed on desktop/mobile; screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
