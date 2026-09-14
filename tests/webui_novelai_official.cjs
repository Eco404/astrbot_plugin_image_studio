/* Run against the isolated harness. Settings, quota and generation are mocked. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-novelai-official-"));
const modelIds = ["nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai-diffusion-5-full", "nai-diffusion-5-curated"];

async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => {
    input.value = next;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function matrix(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  await context.addInitScript(() => {
    window.__quotaTimeOffset = 0;
    const now = Date.now.bind(Date);
    Date.now = () => now() + window.__quotaTimeOffset;
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [], generations = [], discoveries = [], uploads = [];
  let settings, officialId = "", quotaCalls = 0, saves = 0, thirdPartyEnabled = true;
  let quota = { kind: "novelai_official", subscription_active: true, tier: 3, remaining: 1234, subscription_anlas: 1000, purchased_anlas: 234, usage: { percent: 0, is_negative: false, time_until_next_percent: 20 }, checked_at: 1789320000 };
  page.on("pageerror", error => errors.push(error.message));
  try {
    settings = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    await page.route("**/settings/get", route => route.fulfill({ json: structuredClone(settings) }));
    await page.route("**/settings/save", async route => {
      const draft = route.request().postDataJSON();
      settings = { base: draft.base, webui: draft.studio, validation_errors: [] };
      settings.webui.revision = Number(settings.webui.revision || 0) + 1;
      saves++;
      await route.fulfill({ json: { ok: true, settings_revision: settings.webui.revision } });
    });
    await page.route("**/provider/models", async route => {
      discoveries.push(route.request().postDataJSON());
      await route.fulfill({ json: { models: [] } });
    });
    await page.route("**/studio/bootstrap", async route => {
      const response = await route.fetch(), payload = await response.json();
      const official = settings.webui.providers.find(provider => provider.kind === "novelai_official");
      if (official) {
        payload.providers.push(structuredClone(official));
        payload.models.push(...official.models.map(model => ({
          ...structuredClone(model), provider_id: official.id, provider_name: official.name,
          provider_kind: official.kind, model_ref: `${official.id}:${model.id}`,
        })));
      }
      await route.fulfill({ response, json: payload });
    });
    await page.route("**/studio/provider-quota?*", async route => {
      const id = new URL(route.request().url()).searchParams.get("provider_id");
      quotaCalls++;
      await route.fulfill({ json: id === officialId ? { provider_id: id, ...quota } : { provider_id: id, enabled: thirdPartyEnabled, remaining: 943, checked_at: 1789320000 } });
    });
    await page.route("**/studio/generate", async route => {
      generations.push(route.request().postDataJSON());
      await route.fulfill({ json: { images: [], provider_name: "NovelAI 官方测试", model: modelIds[2], elapsed_ms: 1 } });
    });
    const referenceBytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=", "base64");
    await page.route("**/studio/reference/upload", async route => {
      uploads.push(route.request().url());
      await route.fulfill({ json: { id: "official-reference", preview_data_url: `data:image/png;base64,${referenceBytes.toString("base64")}` } });
    });
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1, "use only the isolated harness");
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="nai"]').click();
    assert.match(await frame.locator("#providerForm").textContent(), /toUserId/);
    assert.equal(await frame.locator('[data-model-field="supports_img2img"]').isDisabled(), true, "third-party remains text-only");
    await frame.locator("#addProviderButton").click();
    await choose(frame, '[data-provider-field="kind"]', "novelai_official");
    officialId = await frame.locator('[data-provider-field="id"]').inputValue();
    assert.equal(await frame.locator('[data-provider-field="base_url"]').inputValue(), "https://image.novelai.net");
    assert.equal(await frame.locator('[data-provider-field="max_concurrent_generations"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-provider-field="max_concurrent_generations"]').isDisabled(), true);
    assert.equal(await frame.locator("#discoverModelsButton").count(), 0);
    assert.match(await frame.locator("#providerForm").textContent(), /完整 Persistent API Token/);
    assert.equal(await frame.locator('[data-provider-field="api_key"]').getAttribute("type"), "text");
    assert.deepEqual(await frame.locator("#newModelChoices option").evaluateAll(options => options.map(option => option.value)), modelIds);
    for (const modelId of modelIds) {
      await frame.locator("#newModelChoice").fill(modelId);
      await frame.locator("#addModelButton").click();
      assert.equal(await frame.locator('[data-model-field="supports_img2img"]').isChecked(), true);
      assert.equal(await frame.locator('[data-model-field="supports_img2img"]').isDisabled(), false);
    }
    await frame.locator(`[data-settings-model="${modelIds[2]}"]`).click();
    const expected = { size: "1024x1024", count: 1, seed: -1, steps: 28, scale: 5, cfg_rescale: 0, sampler: "k_euler_ancestral", noise_schedule: "karras", image_format: "png", strength: 0.7, noise: 0 };
    const schema = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    for (const [key, value] of Object.entries(expected)) {
      assert.equal(schema[key].default, value, `${key} default`);
      assert.equal(schema[key].webui_visible, true);
      assert.equal(schema[key].record_in_history, true);
      assert.equal(schema[key].refill_from_history, key !== "count");
    }
    assert.deepEqual(schema.strength.modes, ["img2img"]);
    assert.deepEqual(schema.noise.modes, ["img2img"]);
    assert.equal(schema.params_version, undefined);
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').isDisabled(), false);
    assert.equal(await frame.locator('[data-model-field="max_concurrent_requests"]').inputValue(), "8");
    assert.match(await frame.locator("#modelForm").textContent(), /多样本.*Anlas/);
    await frame.locator('[data-model-field="native_batch_size"]').fill("2");
    await frame.locator('[data-model-field="supports_img2img"]').locator("..").click();
    assert.equal(await frame.locator('[data-model-field="supports_img2img"]').isChecked(), false, "官方模型可以手动关闭图生图");
    await frame.locator('[data-model-field="supports_img2img"]').locator("..").click();
    assert.equal(await frame.locator('[data-model-field="supports_img2img"]').isChecked(), true, "官方模型默认支持单底图图生图");
    assert.equal(await frame.locator('[data-model-field="max_reference_images"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-model-field="max_reference_images"]').isDisabled(), true);
    await frame.locator('[data-model-tab="tool"]').click();
    assert.match(await frame.locator('[data-model-field="tool_prompt_instructions"]').inputValue(), /标签.*自然语言/);
    assert.equal(await frame.locator('[data-tool-field="max_reference_images"]').getAttribute("max"), "1");
    assert.equal(await frame.locator('[data-edit-tool-parameter="params_version"]').count(), 0);
    await frame.locator("#saveSettingsButton").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    assert.equal(saves, 1);
    const savedProvider = settings.webui.providers.find(provider => provider.id === officialId);
    assert.equal(savedProvider.max_concurrent_generations, 1);
    assert.equal(savedProvider.models.length, 4);
    assert.ok(savedProvider.models.every(model => model.capability_source === "builtin" && model.max_reference_images === 1));
    assert.equal(savedProvider.models.find(model => model.id === modelIds[2]).native_batch_size, 2);
    assert.deepEqual(discoveries, []);

    await frame.locator('[data-view="generate"]').click();
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[2]}`);
    await frame.locator("#providerQuota").filter({ hasText: "Anlas 1,234 · V5 0%" }).waitFor();
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), false, "0% is not exhaustion");
    assert.match(await frame.locator("#providerQuota").getAttribute("title"), /0%（可用）/);
    assert.equal(await frame.locator('[data-model-parameter="strength"]').count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="noise"]').count(), 0);
    await frame.locator("#prompt").fill("mountain landscape, daylight");
    await frame.locator('[data-model-parameter="count"]').fill("1");
    const beforeGeneration = quotaCalls;
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => !document.getElementById("generateButton").disabled);
    await frame.locator("#providerQuota").filter({ hasText: "Anlas 1,234" }).waitFor();
    assert.equal(quotaCalls, beforeGeneration + 1, "generation refreshes official quota");
    assert.equal(generations[0].count, 1);
    assert.equal(generations[0].parameters.strength, undefined);
    assert.equal(generations[0].parameters.noise, undefined);
    assert.equal(generations[0].parameters.params_version, undefined);

    async function refreshQuota(next, expectedText) {
      quota = { ...quota, ...next };
      await frame.evaluate(() => { window.__quotaTimeOffset += 31000; window.dispatchEvent(new Event("focus")); });
      await frame.locator("#providerQuota").filter({ hasText: expectedText }).waitFor();
    }
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[0]}`);
    await refreshQuota({ subscription_active: false, tier: 0, remaining: 0, subscription_anlas: 0, purchased_anlas: 0, usage: null }, "Anlas 0 · 未订阅");
    assert.equal(await frame.locator("#providerQuota").textContent(), "Anlas 0 · 未订阅");
    assert.doesNotMatch(await frame.locator("#providerQuota").getAttribute("title"), /V5/);
    assert.match(await frame.locator("#providerQuota").getAttribute("title"), /Anlas 余额不代表免费试用剩余次数/);
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), false, "no subscription is informative, not a disabled provider");
    assert.equal(await frame.locator("#generateButton").isEnabled(), true);
    await frame.evaluate(() => { window.__dismissImageStudioNotice(); window.scrollTo({ top: 0, behavior: "instant" }); });
    await page.screenshot({ path: path.join(output, `${engine}-${width}-unsubscribed.png`) });
    const beforeModelSwitch = quotaCalls;
    for (const modelId of modelIds) {
      await choose(frame, "#modelChoice", `${officialId}:${modelId}`);
      const expectedQuota = modelId.startsWith("nai-diffusion-5-") ? "Anlas 0 · V5 未知 · 未订阅" : "Anlas 0 · 未订阅";
      assert.equal(await frame.locator("#providerQuota").textContent(), expectedQuota);
      assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), false);
    }
    assert.equal(quotaCalls, beforeModelSwitch, "model changes reuse quota while adapting the visible fields");
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[2]}`);
    await refreshQuota({ subscription_active: true, tier: 3 }, "Anlas 0 · V5 未知");
    assert.doesNotMatch(await frame.locator("#providerQuota").textContent(), /未订阅/);
    assert.match(await frame.locator("#providerQuota").getAttribute("title"), /订阅：有效/);
    await refreshQuota({ tier: null, remaining: null, subscription_anlas: null, purchased_anlas: null, usage: null }, "Anlas 未知 · V5 未知");
    assert.equal(await frame.locator("#generateButton").isEnabled(), true);
    await refreshQuota({ remaining: 0, usage: { percent: 0, is_negative: false, time_until_next_percent: null } }, "Anlas 0 · V5 0%");
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), false);
    await refreshQuota({ usage: { percent: -1, is_negative: true, time_until_next_percent: 50 } }, "Anlas 0 · V5 已用尽");
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), true);
    assert.equal(await frame.locator("#generateButton").isEnabled(), true, "quota is informative");
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[0]}`);
    assert.equal(await frame.locator("#providerQuota").textContent(), "Anlas 0");
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), false, "V5 exhaustion does not apply to V4.5");
    assert.doesNotMatch(await frame.locator("#providerQuota").getAttribute("title"), /V5|下一个百分比/);
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[2]}`);
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), true);
    await refreshQuota({ remaining: -1 }, "额度暂不可用");
    assert.equal(await frame.locator("#generateButton").isEnabled(), true);
    await refreshQuota({ remaining: 1234, usage: { percent: 23, is_negative: false, time_until_next_percent: 10 } }, "Anlas 1,234 · V5 23%");
    await frame.locator('[data-mode="img2img"]').click();
    await choose(frame, "#modelChoice", `${officialId}:${modelIds[2]}`);
    assert.equal(await frame.locator('[data-model-parameter="strength"]').inputValue(), "0.7");
    assert.equal(await frame.locator('[data-model-parameter="noise"]').inputValue(), "0");
    assert.equal(await frame.locator("#referenceField").isVisible(), true);
    await frame.locator("#referenceUpload").setInputFiles([
      { name: "reference-one.png", mimeType: "image/png", buffer: referenceBytes },
      { name: "reference-two.png", mimeType: "image/png", buffer: referenceBytes },
    ]);
    await frame.locator(".reference-item").waitFor();
    assert.equal(await frame.locator(".reference-item").count(), 1);
    assert.equal(await frame.locator("#referenceUpload").isDisabled(), true);
    assert.equal(uploads.length, 1, "only one image is uploaded");
    await frame.locator('[data-model-parameter="strength"]').fill("0.45");
    await frame.locator('[data-model-parameter="noise"]').fill("0.2");
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => !document.getElementById("generateButton").disabled);
    assert.equal(generations[1].mode, "img2img");
    assert.equal(generations[1].parameters.strength, 0.45);
    assert.equal(generations[1].parameters.noise, 0.2);
    assert.deepEqual(generations[1].reference_ids, ["official-reference"]);
    const bounds = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(bounds.scroll <= bounds.width + 1, `horizontal overflow: ${JSON.stringify(bounds)}`);
    await frame.evaluate(() => { window.__dismissImageStudioNotice(); window.scrollTo({ top: 0, behavior: "instant" }); });
    await page.screenshot({ path: path.join(output, `${engine}-${width}-official.png`) });
    await frame.locator('[data-mode="text2img"]').click();
    await choose(frame, "#modelChoice", "nai:nai-diffusion-4-5-full");
    await frame.locator("#providerQuota").filter({ hasText: "剩余额度 943" }).waitFor();
    thirdPartyEnabled = false;
    await refreshQuota({}, "剩余额度 943 · 已停用");
    assert.equal(await frame.locator("#providerQuota").evaluate(element => element.classList.contains("is-warning")), true, "third-party account enablement keeps its existing meaning");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: official settings, defaults, capability limits, mode parameters and independent quota passed`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await matrix(browser, width); }
  finally { await browser.close(); }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
