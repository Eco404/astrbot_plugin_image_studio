/* Use only tests.support.webui_harness: settings, uploads and generation use isolated data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-reference-limits-"));

async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => {
    input.value = next;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function matrix(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const providerId = `reference-limits-${engine}-${width}-${Date.now()}`;
  const ref = id => `${providerId}:${id}`;
  let frame;
  let originalDefaults;
  const settings = async () => (await page.request.get(`${apiRoot}/settings/get`)).json();
  async function saveApi(value) {
    const response = await page.request.post(`${apiRoot}/settings/save`, { data: { base: value.base, studio: value.webui, settings_revision: value.webui.revision } });
    assert.equal(response.status(), 200, await response.text());
  }
  async function edit(id = "legacy") {
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${providerId}"]`).click();
    await frame.locator(`[data-settings-model="${id}"]`).click();
    await frame.locator('[data-model-tab="model"]').click();
  }
  async function toggle(selector, value) {
    const input = frame.locator(selector);
    if (await input.isChecked() !== value) await input.locator("..").click();
    assert.equal(await input.isChecked(), value);
  }
  async function available(selector, modelRef) {
    return frame.locator(selector).evaluate((input, target) => Array.from(input.options).some(option => option.value === target), modelRef);
  }
  async function savePage() {
    const saved = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/settings/save"));
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await saved).status(), 200);
    await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
  }
  async function number(selector, value, expected = value) {
    await frame.locator(selector).fill(String(value));
    await frame.locator(selector).blur();
    assert.equal(await frame.locator(selector).inputValue(), String(expected));
  }
  const modelLimit = '[data-model-field="max_reference_images"]';
  const toolLimit = '[data-tool-field="max_reference_images"]';
  const support = '[data-model-field="supports_img2img"]';
  const toolDefault = "#settingToolDefaultImageModel";
  const pageDefault = "#settingPageDefaultImageModel";
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/, "isolated harness required");
    const initial = await settings();
    originalDefaults = structuredClone(initial.webui.generation_defaults);
    initial.webui.providers.push({ id: providerId, name: "Reference limit fixture", kind: "custom_json", base_url: "https://example.test", enabled: true, models: [
      { id: "legacy", name: "Legacy tool zero", supports_text2img: true, supports_img2img: true, max_reference_images: 8, capability_source: "manual", tool: { enabled: true, max_reference_images: 0 } },
      { id: "zero", name: "Legacy capability zero", supports_text2img: true, supports_img2img: true, max_reference_images: 0, capability_source: "unknown", tool: { enabled: true, max_reference_images: 0 } },
    ] });
    await saveApi(initial);
    const stored = (await settings()).webui.providers.find(provider => provider.id === providerId).models;
    assert.equal(stored[0].tool.max_reference_images, 1);
    assert.equal(stored[1].max_reference_images, 1);
    assert.equal(stored[1].tool.max_reference_images, 1);

    // A stale settings response must not revive zero-as-disabled behavior in the browser.
    await page.route("**/settings/get", async route => {
      const response = await route.fetch();
      const payload = await response.json();
      const models = payload.webui.providers.find(provider => provider.id === providerId)?.models;
      if (models) { models[0].tool.max_reference_images = 0; models[1].max_reference_images = 0; models[1].tool.max_reference_images = 0; }
      await route.fulfill({ response, json: payload });
    });
    await page.goto(base);
    await page.locator("#studio").waitFor();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await edit();
    await page.unroute("**/settings/get");
    assert.equal(await frame.locator(modelLimit).getAttribute("min"), "1");
    assert.equal(await available(toolDefault, ref("legacy")), true);
    assert.equal(await available(toolDefault, ref("zero")), true);
    const originalPageDefault = await frame.locator(pageDefault).inputValue();
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator(toolLimit).inputValue(), "1");
    assert.equal(await frame.locator(toolLimit).getAttribute("min"), "1");
    await number(toolLimit, 4);
    await choose(frame, toolDefault, ref("legacy"));
    await frame.locator('[data-model-tab="model"]').click();
    await frame.locator(modelLimit).fill("");
    assert.equal(await frame.locator(modelLimit).inputValue(), "", "typing must not force a premature 1");
    await number(modelLimit, 8);
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator(toolLimit).inputValue(), "4", "retyping a model limit preserves the tool limit");
    await frame.locator('[data-model-tab="model"]').click();
    await toggle(support, false);
    assert.equal(await available(toolDefault, ref("legacy")), false);
    assert.equal(await available(pageDefault, ref("legacy")), false);
    assert.equal(await frame.locator(toolDefault).inputValue(), "", "invalid defaults clear immediately");
    await toggle(support, true);
    assert.equal(await frame.locator(modelLimit).inputValue(), "8");
    assert.equal(await available(toolDefault, ref("legacy")), true);
    assert.equal(await frame.locator(toolDefault).inputValue(), "", "reenabling must not select a default automatically");
    assert.equal(await frame.locator(pageDefault).inputValue(), originalPageDefault, "unaffected defaults survive draft refresh");
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator(toolLimit).inputValue(), "4", "off/on preserves the tool limit");
    await toggle('[data-model-field="tool_enabled"]', false);
    assert.equal(await available(toolDefault, ref("legacy")), false);
    assert.equal(await available(pageDefault, ref("legacy")), true);
    await toggle('[data-model-field="tool_enabled"]', true);
    assert.equal(await available(toolDefault, ref("legacy")), true);
    await number(toolLimit, 0, 1);
    await number(toolLimit, 12, 8);
    await frame.locator('[data-model-tab="model"]').click();
    await number(modelLimit, 3);
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator(toolLimit).inputValue(), "3");
    assert.equal(await frame.locator(toolLimit).getAttribute("max"), "3");
    await page.screenshot({ path: path.join(output, `${engine}-${width}-tool-limit.png`) });
    await edit("zero");
    assert.equal(await frame.locator(modelLimit).inputValue(), "1");
    assert.equal(await frame.locator(modelLimit).isEnabled(), true);
    await number(modelLimit, 0, 1);

    const discovered = [
      { id: "unknown", name: "Unknown reference capacity", supports_text2img: true, supports_img2img: true, max_reference_images: 0, capability_source: "unknown" },
      { id: "known", name: "Known reference capacity", supports_text2img: true, supports_img2img: true, max_reference_images: 6, capability_source: "remote" },
    ];
    await page.route("**/provider/models", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ models: discovered }) }));
    await frame.locator("#discoverModelsButton").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "已获取 2 个模型" }).waitFor();
    await page.unroute("**/provider/models");
    for (const id of ["unknown", "manual", "known"]) {
      await frame.locator("#newModelChoice").fill(id);
      await frame.locator("#addModelButton").click();
      assert.equal(await frame.locator(support).isChecked(), id === "known");
      assert.equal(await available(toolDefault, ref(id)), id === "known");
      if (id !== "known") {
        assert.equal(await frame.locator(modelLimit).count(), 0);
        await toggle(support, true);
        assert.equal(await frame.locator(modelLimit).inputValue(), "1");
        assert.equal(await frame.locator(modelLimit).isEnabled(), true);
        await number(modelLimit, 2);
      } else {
        assert.equal(await frame.locator(modelLimit).inputValue(), "6");
        assert.equal(await frame.locator(modelLimit).isDisabled(), true);
      }
      await frame.locator('[data-model-tab="tool"]').click();
      assert.equal(await frame.locator(toolLimit).inputValue(), id === "known" ? "6" : "1");
      await frame.locator('[data-model-tab="model"]').click();
    }
    await frame.locator('[data-settings-provider="nai"]').click();
    assert.equal(await frame.locator(support).isChecked(), false);
    assert.equal(await frame.locator(support).isDisabled(), true);
    await edit("unknown");
    await choose(frame, toolDefault, ref("unknown"));
    await choose(frame, pageDefault, ref("unknown"));
    await frame.locator('[data-default-scope="tool"]').click();
    await frame.locator(toolDefault).locator("..").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${engine}-${width}-draft-default.png`) });
    await savePage();
    const saved = await settings();
    assert.equal(saved.webui.generation_defaults.tool.img2img_model_ref, ref("unknown"), "newly enabled defaults save in one submission");
    const model = saved.webui.providers.find(provider => provider.id === providerId).models.find(item => item.id === "unknown");
    assert.equal(model.supports_img2img, true);
    assert.equal(model.max_reference_images, 2);
    assert.equal(model.tool.max_reference_images, 1);
    await frame.locator('[data-view="generate"]').click();
    await frame.locator('[data-mode="img2img"]').click();
    await choose(frame, "#modelChoice", ref("unknown"));
    assert.equal(await frame.locator("#referenceCount").innerText(), "0/2 张");
    const listing = await (await page.request.get(`${apiRoot}/gallery/list?limit=1`)).json();
    const buffer = await (await page.request.get(`${apiRoot}/gallery/download/${listing.items[0].image_id}`)).body();
    await frame.locator("#referenceUpload").setInputFiles(Array.from({ length: 3 }, (_, index) => ({ name: `reference-${index}.png`, mimeType: "image/png", buffer })));
    await frame.waitForFunction(() => document.getElementById("referenceCount").textContent === "2/2 张" && document.getElementById("referenceChooseButton").getAttribute("aria-busy") === "false");
    assert.equal(await frame.locator("#referenceChooseButton").isDisabled(), true);
    await frame.locator("#prompt").fill("Reference limit regression");
    const generated = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/studio/generate"));
    await frame.locator("#generateButton").click();
    const response = await generated;
    assert.equal(response.status(), 200, await response.text());
    const result = await response.json();
    const detail = await (await page.request.get(`${apiRoot}/gallery/detail/${result.generation_id}`)).json();
    assert.equal(detail.mode, "img2img");
    assert.equal(detail.references.length, 2);
    await page.screenshot({ path: path.join(output, `${engine}-${width}-generation.png`) });
    const geometry = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(geometry.scroll <= geometry.width + 1, JSON.stringify(geometry));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ engine, width, passed: true }));
  } finally {
    const cleanup = await settings();
    cleanup.webui.providers = cleanup.webui.providers.filter(provider => provider.id !== providerId);
    if (originalDefaults) cleanup.webui.generation_defaults = originalDefaults;
    await saveApi(cleanup);
    await context.close();
  }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await matrix(browser, width); }
  finally { await browser.close(); }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
