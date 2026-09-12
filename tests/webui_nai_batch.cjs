/* Run against an isolated webui_harness.py instance; generation is intercepted. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18801";
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-nai-batch-"));

async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => {
    input.value = next;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function saveSettings(frame) {
  await frame.locator("#saveSettingsButton").click();
  await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
  await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
}

async function matrix(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [], requests = [];
  page.on("pageerror", error => errors.push(error.message));
  const modelId = `batch-${engine}-${width}-${Date.now()}`;
  const modelRef = `nai:${modelId}`;
  let partial = true;
  const warning = "本批请求 3 张，成功 2 张，失败 1 张：第 2 张请求超时。";
  try {
    const listing = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=1`)).json();
    const image = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/download/${listing.items[0].image_id}`)).body();
    const dataUrl = `data:image/png;base64,${image.toString("base64")}`;
    await page.route("**/studio/generate", async route => {
      requests.push(route.request().postDataJSON());
      await route.fulfill({ json: {
        images: Array.from({ length: partial ? 2 : 3 }, () => ({ data_url: dataUrl })),
        provider_name: "NAI 测试", model: modelId, elapsed_ms: 10,
        generation_id: "isolated-browser-batch", warning: partial ? warning : "",
      } });
    });
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1, "only use the isolated harness");
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="nai"]').click();
    await frame.locator('[data-settings-model="nai-diffusion-4-5-full"]').click();
    for (const key of ["count"]) {
      const input = frame.locator(`[data-schema-default="${key}"]`);
      assert.equal(await input.inputValue(), "1", `${key}: existing models receive the missing default`);
      assert.equal(await input.getAttribute("type"), "number");
    }
    await frame.locator("#newModelChoice").fill(modelId);
    await frame.locator("#addModelButton").click();
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').isDisabled(), true);
    assert.equal(await frame.locator('[data-model-field="max_concurrent_requests"]').inputValue(), "8");
    for (const key of ["count"]) {
      const input = frame.locator(`[data-schema-default="${key}"]`);
      assert.equal(await input.inputValue(), "1", `${key}: new models default to one`);
      for (const [attribute, value] of Object.entries({ type: "number", min: "1", max: "16", step: "1" })) {
        assert.equal(await input.getAttribute(attribute), value, `${key}: ${attribute}`);
      }
      assert.match(await input.locator("..").locator("label").getAttribute("title"), /取值范围.*1.*16/);
    }
    await frame.locator('[data-schema-default="count"]').fill("3");
    await frame.locator('[data-schema-default="count"]').blur();
    await frame.locator('[data-model-field="max_concurrent_requests"]').fill("2");
    await frame.locator('[data-model-field="max_concurrent_requests"]').blur();
    assert.equal(await frame.locator('[data-schema-default="concurrency"]').count(), 0);
    await frame.locator('[data-model-tab="tool"]').click();
    for (const [key, value] of [["count", "4"]]) {
      await frame.locator(`[data-edit-tool-parameter="${key}"]`).click();
      assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "");
      assert.match(await frame.locator("#toolParameterDescription").inputValue(), /取值范围.*1.*16/);
      await frame.locator("#toolParameterDefault").fill(value);
      await frame.locator("#parameterDialogApply").click();
    }
    assert.equal(await frame.locator('[data-edit-tool-parameter="concurrency"]').count(), 0);
    await saveSettings(frame);
    const saved = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    const model = saved.webui.providers.find(provider => provider.id === "nai").models.find(item => item.id === modelId);
    assert.equal(model.parameters.count.default, 3);
    assert.equal(model.max_concurrent_requests, 2);
    assert.equal(model.tool.parameters.count.default_override, 4);
    assert.equal(model.tool.parameters.concurrency, undefined);
    await choose(frame, "#settingPageDefaultTextModel", modelRef);
    await saveSettings(frame);

    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#modelChoice").inputValue(), modelRef);
    const count = frame.locator('[data-model-parameter="count"]');
    assert.equal(await count.inputValue(), "3", "page uses model default, not tool override");
    assert.equal(await frame.locator('[data-model-parameter="concurrency"]').count(), 0);
    assert.equal(await count.getAttribute("type"), "number", "integer schema must render a numeric input");
    await count.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${engine}-${width}-parameters.png`) });
    await frame.locator("#prompt").fill("mountain landscape, daylight");
    for (const invalid of ["0", "17", "1.5"]) {
      await count.fill(invalid);
      assert.equal(await count.evaluate(input => input.checkValidity()), false, `count=${invalid}`);
      await frame.locator("#generateButton").click();
      assert.equal(requests.length, 0, "invalid counts cannot submit");
    }
    await count.fill("3");
    await frame.locator("#generateButton").click();
    await frame.locator("#generationError").filter({ hasText: warning }).waitFor();
    assert.equal(await frame.locator("#appNoticeMessage").textContent(), warning);
    assert.equal(await frame.locator(".result-card").count(), 2, "partial failures retain all successful images");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].count, 3);
    assert.equal(requests[0].parameters.concurrency, undefined);
    assert.equal("count" in requests[0].parameters, false, "count uses the top-level request field");
    await frame.locator("#resultGrid").scrollIntoViewIfNeeded();
    await frame.waitForFunction(() => [...document.querySelectorAll(".result-image")].every(image => image.complete && image.naturalWidth > 0));
    await page.screenshot({ path: path.join(output, `${engine}-${width}-partial.png`) });
    const bounds = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(bounds.scroll <= bounds.width + 1, `page overflow: ${JSON.stringify(bounds)}`);
    partial = false;
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => document.querySelectorAll(".result-card").length === 3);
    assert.equal(await frame.locator("#generationError").textContent(), "", "new successful request clears previous warning");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: existing/new defaults, persisted page/tool values, integer bounds, request mapping and partial results passed`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const width of [1440, 390]) await matrix(browser, width);
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
