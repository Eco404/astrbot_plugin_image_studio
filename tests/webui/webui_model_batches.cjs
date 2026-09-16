/* Real WebUI/API/service/store flow. Use only the isolated fake-provider harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-model-batches-"));

async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => {
    input.value = next;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function saveSettings(frame, page) {
  const saved = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/settings/save"));
  await frame.locator("#saveSettingsButton").click();
  assert.equal((await saved).status(), 200);
  await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
  await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
}

async function matrix(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const key = `${engine}-${width}-${Date.now()}`;
  let frame;
  async function reload() {
    await page.goto(base);
    await page.locator("#studio").waitFor();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
  }
  async function edit(providerId, modelId) {
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${providerId}"]`).click();
    if (modelId) await frame.locator(`[data-settings-model="${modelId}"]`).click();
    if (await frame.locator('[data-model-tab="model"]').count()) await frame.locator('[data-model-tab="model"]').click();
  }
  async function generate(modelRef, expectedCounts, countKey = "count") {
    const before = await (await page.request.get(`${apiRoot}/gallery/list?limit=1`)).json();
    const statsBefore = await (await page.request.get(`${base}/_harness/generation-stats`)).json();
    await frame.locator('[data-view="generate"]').click();
    await choose(frame, "#modelChoice", modelRef);
    const expected = expectedCounts.reduce((total, count) => total + count, 0);
    await frame.locator(`[data-model-parameter="${countKey}"]`).fill(String(expected));
    for (const name of ["concurrency", "native_batch_size", "max_concurrent_requests"]) assert.equal(await frame.locator(`[data-model-parameter="${name}"]`).count(), 0);
    await frame.locator("#prompt").fill(`Isolated ${modelRef} batch verification`);
    const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/studio/generate"));
    await frame.locator("#generateButton").click();
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.images.length, expected, JSON.stringify(result));
    await frame.waitForFunction(count => document.querySelectorAll(".result-card").length === count, expected);
    const after = await (await page.request.get(`${apiRoot}/gallery/list?limit=1`)).json();
    assert.equal(after.total, before.total + 1, "a batch creates exactly one gallery record");
    const detail = await (await page.request.get(`${apiRoot}/gallery/detail/${result.generation_id}`)).json();
    assert.equal(detail.images.length, expected);
    const statsAfter = await (await page.request.get(`${base}/_harness/generation-stats`)).json();
    const calls = statsAfter.calls.slice(statsBefore.calls.length);
    assert.deepEqual(calls.map(call => call.count), expectedCounts);
    const forbidden = ["concurrency", "batch_mode", "native_batch_size", "max_concurrent_requests"];
    assert.ok(calls.every(call => forbidden.every(name => !Object.hasOwn(call.parameters, name))), "scheduler fields must not reach the provider executor");
    assert.equal(Math.max(...calls.map(call => call.active)), Math.min(2, expectedCounts.length), "model concurrency is honored even with a larger provider limit");
    return result;
  }
  try {
    const home = await page.request.get(base);
    assert.match(await home.text(), /<iframe id="studio"/, "only use the isolated harness");
    const settings = await (await page.request.get(`${apiRoot}/settings/get`)).json();
    settings.webui.history.max_records = 0;
    settings.webui.history.max_megabytes = 0;
    const providers = settings.webui.providers;
    for (const kind of ["gemini", "custom_json"]) providers.push({ id: `fixture-${kind}-${key}`, name: kind, kind, base_url: "https://example.test", enabled: true, models: [], max_concurrent_generations: 16 });
    for (const provider of providers) provider.max_concurrent_generations = 16;
    const seeded = await page.request.post(`${apiRoot}/settings/save`, { data: { base: settings.base, studio: settings.webui, settings_revision: settings.webui.revision } });
    assert.equal(seeded.status(), 200, await seeded.text());
    await reload();
    for (const kind of ["openai_images", "gemini", "custom_json", "nai_direct"]) {
      const providerId = kind === "openai_images" ? "natural" : kind === "nai_direct" ? "nai" : `fixture-${kind}-${key}`;
      const modelId = `batch-${kind}-${key}`;
      const modelRef = `${providerId}:${modelId}`;
      await edit(providerId);
      await frame.locator("#newModelChoice").fill(modelId);
      await frame.locator("#addModelButton").click();
      assert.equal(await frame.locator('[data-model-field="batch_mode"]').count(), 0);
      assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "1");
      assert.equal(await frame.locator('[data-model-field="native_batch_size"]').isDisabled(), kind === "nai_direct");
      assert.equal(await frame.locator('[data-model-field="max_concurrent_requests"]').inputValue(), "8");
      assert.equal(await frame.locator('[data-schema-default="count"]').inputValue(), "1");
      assert.equal(await frame.locator('[data-schema-default="concurrency"]').count(), 0);
      assert.equal(await frame.locator('[data-schema-default="count"]').getAttribute("max"), ["gemini", "nai_direct"].includes(kind) ? "16" : "4", "existing count bounds are retained");
      await frame.locator('[data-model-field="max_concurrent_requests"]').fill("2");
      await frame.locator('[data-model-field="max_concurrent_requests"]').blur();
      if (kind !== "nai_direct") {
        await frame.locator("#discoverModelsButton").click();
        await frame.locator("#appNoticeMessage").filter({ hasText: "已获取" }).waitFor();
        assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "4", "remote discovery fills models without manual capacity");
        const discoveredId = `discovered-${kind}`;
        if (!await frame.locator(`[data-settings-model="${discoveredId}"]`).count()) {
          await frame.locator("#newModelChoice").fill(discoveredId);
          await frame.locator("#addModelButton").click();
          assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "4", "new models use discovered native capacity");
          assert.equal(await frame.locator('[data-model-field="max_concurrent_requests"]').inputValue(), "8");
          await frame.locator(`[data-settings-model="${modelId}"]`).click();
        }
        await frame.locator('[data-model-field="native_batch_size"]').fill("3");
        await frame.locator('[data-model-field="native_batch_size"]').blur();
        await frame.locator("#discoverModelsButton").click();
        await frame.locator("#appNoticeMessage").filter({ hasText: "已获取" }).waitFor();
        assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "3", "remote discovery must preserve manually entered capacity");
        await frame.locator('[data-model-field="native_batch_size"]').fill("4");
        await frame.locator('[data-model-field="native_batch_size"]').blur();
      }
      const countKey = kind === "custom_json" ? "samples" : "count";
      const total = kind === "nai_direct" ? 3 : 10;
      const schema = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
      schema[countKey] = { ...schema.count, default: total, max: 16, request_key: countKey === "samples" ? "n" : "count" };
      if (countKey !== "count") delete schema.count;
      await frame.locator(".schema-raw summary").click();
      await frame.locator("#modelParametersSchema").fill(JSON.stringify(schema));
      await frame.locator("#modelParametersSchema").blur();
      await frame.locator(".schema-raw summary").click();
      await frame.locator('[data-model-field="native_batch_size"]').scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${kind}-settings.png`) });
      await frame.locator('[data-model-tab="tool"]').click();
      for (const name of ["batch_mode", "concurrency", "native_batch_size", "max_concurrent_requests"]) assert.equal(await frame.locator(`[data-edit-tool-parameter="${name}"]`).count(), 0);
      await frame.locator(`[data-edit-tool-parameter="${countKey}"]`).click();
      await frame.locator("#toolParameterDefault").fill("2");
      await frame.locator("#parameterDialogApply").click();
      await saveSettings(frame, page);
      await choose(frame, "#settingPageDefaultTextModel", modelRef);
      await saveSettings(frame, page);
      const saved = await (await page.request.get(`${apiRoot}/settings/get`)).json();
      const model = saved.webui.providers.find(item => item.id === providerId).models.find(item => item.id === modelId);
      assert.equal(model.native_batch_size, kind === "nai_direct" ? 1 : 4);
      assert.equal(model.native_batch_size_source, kind === "nai_direct" ? "fixed" : "manual");
      assert.equal(model.max_concurrent_requests, 2);
      assert.equal(model.parameters[countKey].default, total);
      assert.equal(model.tool.parameters[countKey].default_override, 2);
      await reload();
      assert.equal(await frame.locator(`[data-model-parameter="${countKey}"]`).inputValue(), String(total));
      await frame.locator(`[data-model-parameter="${countKey}"]`).scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${kind}-generate.png`) });
      const result = await generate(modelRef, kind === "nai_direct" ? [1, 1, 1] : [4, 4, 2], countKey);
      await edit(providerId, modelId);
      await frame.locator(`[data-schema-default="${countKey}"]`).fill("5");
      await frame.locator(`[data-schema-default="${countKey}"]`).blur();
      await saveSettings(frame, page);
      const external = await (await page.request.get(`${apiRoot}/settings/get`)).json();
      const externalModel = external.webui.providers.find(item => item.id === providerId).models.find(item => item.id === modelId);
      externalModel.parameters[countKey].default = 6;
      externalModel.native_batch_size = kind === "nai_direct" ? 1 : 3;
      externalModel.max_concurrent_requests = 4;
      const externalSave = await page.request.post(`${apiRoot}/settings/save`, { data: { base: external.base, webui: external.webui, settings_revision: external.settings_revision } });
      assert.equal(externalSave.status(), 200);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator(`.gallery-card[data-gallery-id="${result.generation_id}"]`).click();
      await frame.locator("#detailReproduce").click();
      await frame.locator("#generateView.is-active").waitFor();
      assert.equal(await frame.locator(`[data-model-parameter="${countKey}"]`).inputValue(), "6", "reproduction refreshes defaults changed outside this browser, not historical count or tool override");
      assert.equal(await frame.locator('[data-model-parameter="concurrency"]').count(), 0);
      const bounds = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      assert.ok(bounds.scroll <= bounds.width + 1, `page overflow: ${JSON.stringify(bounds)}`);
      console.log(`${engine}-${width}-${kind}: model capacity/concurrency, tool defaults, automatic chunks and reproduction defaults passed`);
      await reload();
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const width of [1440, 390]) await matrix(browser, width);
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
