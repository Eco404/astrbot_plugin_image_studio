/* Isolated harness only: verifies persisted mode defaults and parameter policy. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const api = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;

async function change(frame, selector, value) {
  await frame.locator(selector).evaluate((element, next) => {
    element.value = next;
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function run(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 960 }, hasTouch: width < 540 });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const providerId = `mode-defaults-${width}`;
  const modelRef = `${providerId}:batch-paint`;
  let frame;
  async function load() {
    await page.goto(base);
    await page.locator("#studio").waitFor();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${providerId}"]`).click();
  }
  async function pick(mode, scope = "model") {
    await frame.locator(`[data-defaults-mode="${mode}"][data-defaults-scope="${scope}"]`).click();
  }
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/);
    const initial = await (await page.request.get(`${api}/settings/get`)).json();
    initial.webui.providers.push({ id: providerId, name: "Mode defaults fixture", kind: "custom_json", enabled: true, base_url: "https://example.test", models: [{
      id: "batch-paint", name: "Mode defaults", supports_text2img: true, supports_img2img: true,
      parameters: {
        steps: { type: "integer", default: 5, min: 1, max: 10, mode_overrides: { img2img: { default: 8, max: 9 } } },
        quality: { type: "select", default: "standard", choices: ["standard", "high"], mode_overrides: { img2img: { default: "fast", choices: ["fast", "precise"] } } },
        text_body: { type: "json", default: { output: "url" }, request_key: "extra_body", modes: ["text2img"] },
        edit_body: { type: "json", default: { output: "b64_json" }, request_key: "extra_body", modes: ["img2img"] },
        optional_seed: { type: "integer", nullable: true, default: null, min: 0, max: 100, mode_overrides: { img2img: { default: null } } },
        optional_text: { type: "text", nullable: true, default: null },
        optional_json: { type: "json", nullable: true, default: null },
      },
      tool: { parameters: { quality: { exposed: true, default_override: "high", mode_overrides: { img2img: { default_override: "precise" } } }, steps: { exposed: true } } },
    }] });
    initial.webui.generation_defaults.page.text2img_model_ref = modelRef;
    initial.webui.generation_defaults.page.img2img_model_ref = modelRef;
    const saved = await page.request.post(`${api}/settings/save`, { data: { base: initial.base, studio: initial.webui, settings_revision: initial.webui.revision } });
    assert.equal(saved.status(), 200, await saved.text());
    await load();
    assert.equal(await frame.locator('[data-defaults-scope="model"]').count(), 2);
    assert.equal(await frame.locator('[data-schema-default="steps"]').inputValue(), "5");
    assert.equal(await frame.locator('[data-schema-default="edit_body"]').count(), 0);
    await change(frame, '[data-schema-default="steps"]', "6");
    await pick("img2img");
    assert.equal(await frame.locator('[data-schema-default="steps"]').inputValue(), "8");
    assert.equal(await frame.locator('[data-schema-default="steps"]').getAttribute("max"), "9");
    assert.equal(await frame.locator('[data-schema-default="text_body"]').count(), 0);
    assert.equal(await frame.locator('[data-schema-default="quality"]').inputValue(), "fast");
    await change(frame, '[data-schema-default="steps"]', "9");
    await change(frame, '[data-schema-default="quality"]', "precise");
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    await change(frame, "#schemaPolicy-modes", "text2img");
    await frame.locator("#studioModalFooter button").filter({ hasText: "保存" }).click();
    await frame.locator('[data-schema-default="steps"]').waitFor({ state: "detached" });
    assert.equal(await frame.locator('[data-schema-default="steps"]').count(), 0);
    await pick("text2img");
    assert.equal(await frame.locator('[data-schema-default="steps"]').inputValue(), "6");
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    await change(frame, "#schemaPolicy-modes", "common");
    await frame.locator("#studioModalFooter button").filter({ hasText: "保存" }).click();
    await frame.locator('[data-model-tab="tool"]').click();
    await pick("img2img", "tool");
    await frame.locator('[data-edit-tool-parameter="quality"]').click();
    assert.deepEqual(await frame.locator("#toolParameterDefaultChoice option").allTextContents(), ["模型默认值", "fast", "precise"]);
    assert.equal(await frame.locator("#toolParameterDefaultChoice").inputValue(), "1");
    await change(frame, "#toolParameterDefaultChoice", "__model_default__");
    await frame.locator("#parameterDialog").getByRole("button", { name: "应用", exact: true }).click();
    await pick("text2img", "tool");
    await frame.locator('[data-edit-tool-parameter="quality"]').click();
    assert.equal(await frame.locator("#toolParameterDefaultChoice").inputValue(), "1", "editing one mode preserves shared legacy override in the other");
    await frame.locator("#parameterDialog").getByRole("button", { name: "取消", exact: true }).click();
    const response = page.waitForResponse(item => item.url().endsWith("/settings/save") && item.request().method() === "POST");
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await response).status(), 200);
    const after = await (await page.request.get(`${api}/settings/get`)).json();
    const model = after.webui.providers.find(item => item.id === providerId).models[0];
    assert.equal(model.parameters.steps.default, 5);
    assert.equal(model.parameters.steps.mode_overrides.text2img.default, 6);
    assert.equal(model.parameters.steps.mode_overrides.img2img.default, 9);
    assert.equal(model.parameters.quality.mode_overrides.img2img.default, "precise");
    assert.equal(model.tool.parameters.quality.default_override, "high");
    assert.equal(model.tool.parameters.quality.mode_overrides.img2img.use_model_default, true);
    assert.equal(model.parameters.steps.modes, undefined);
    await load();
    await pick("img2img");
    assert.equal(await frame.locator('[data-schema-default="steps"]').inputValue(), "9");
    await frame.locator('[data-view="generate"]').click();
    await change(frame, "#modelChoice", modelRef);
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "6");
    assert.equal(await frame.locator('[data-model-parameter="edit_body"]').count(), 0);
    await frame.locator("#prompt").fill("Mode defaults isolated regression");
    async function generate() {
      const pending = page.waitForResponse(item => item.url().endsWith("/studio/generate"));
      await frame.locator("#generateButton").click();
      const response = await pending;
      assert.equal(response.status(), 200, await response.text());
      const parameters = response.request().postDataJSON().parameters;
      const result = await response.json();
      return { parameters, result };
    }
    const text = await generate();
    for (const name of ["optional_seed", "optional_text", "optional_json"]) assert.equal(text.parameters[name], null, `${name} retains null before editing`);
    let calls = (await (await page.request.get(`${base}/_harness/generation-stats`)).json()).calls;
    assert.deepEqual(calls.at(-1).parameters.extra_body, { output: "url" });
    await frame.locator('[data-mode="img2img"]').click();
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "9");
    assert.equal(await frame.locator('[data-model-parameter="quality"]').inputValue(), "precise");
    assert.equal(await frame.locator('[data-model-parameter="text_body"]').count(), 0);
    async function uploadReference(result) {
      const uploaded = page.waitForResponse(item => item.url().endsWith("/studio/reference/upload"));
      await frame.locator("#referenceUpload").setInputFiles({ name: "reference.png", mimeType: "image/png", buffer: Buffer.from(result.images[0].data_url.split(",")[1], "base64") });
      assert.equal((await uploaded).status(), 200);
      await frame.locator("#referenceCount").filter({ hasText: "1/" }).waitFor();
    }
    await uploadReference(text.result);
    const edit = await generate();
    for (const name of ["optional_seed", "optional_text", "optional_json"]) assert.equal(edit.parameters[name], null, `${name} retains mode null before editing`);
    calls = (await (await page.request.get(`${base}/_harness/generation-stats`)).json()).calls;
    assert.deepEqual(calls.at(-1).parameters.extra_body, { output: "b64_json" });
    await frame.locator('[data-model-parameter="optional_seed"]').fill("4");
    await frame.locator('[data-model-parameter="optional_text"]').fill("manual");
    await uploadReference(edit.result);
    const modified = await generate();
    assert.equal(modified.parameters.optional_seed, 4);
    assert.equal(modified.parameters.optional_text, "manual");
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-model-field="supports_text2img"]').evaluate(element => {
      element.checked = false;
      element.dispatchEvent(new Event("change", { bubbles: true }));
    });
    assert.equal(await frame.locator('[data-defaults-scope="model"]').count(), 0, "single-mode models do not show the switch");
    assert.equal(await frame.locator('[data-schema-default="steps"]').inputValue(), "9");
    async function closePolicy(label = "保存") {
      await frame.locator("#studioModalFooter button").filter({ hasText: label }).click();
      await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    }
    await frame.locator('[data-edit-schema-policy="edit_body"]').click();
    assert.equal(await frame.locator("#schemaPolicy-modes").count(), 0, "image-only models do not offer parameter mode selection");
    await frame.locator("#schemaPolicy-record_in_history").evaluate(element => {
      element.checked = false;
      element.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await closePolicy();
    let draft = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    assert.deepEqual(draft.edit_body.modes, ["img2img"], "editing other policies preserves hidden mode restrictions");
    assert.equal(draft.edit_body.record_in_history, false);
    async function capability(name, enabled) {
      await frame.locator(`[data-model-field="supports_${name}"]`).evaluate((element, checked) => {
        element.checked = checked;
        element.dispatchEvent(new Event("change", { bubbles: true }));
      }, enabled);
    }
    await capability("img2img", false);
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    assert.equal(await frame.locator("#schemaPolicy-modes").count(), 0, "models without either capability do not offer mode selection");
    await closePolicy();
    await capability("text2img", true);
    await frame.locator('[data-edit-schema-policy="text_body"]').click();
    assert.equal(await frame.locator("#schemaPolicy-modes").count(), 0, "text-only models do not offer mode selection");
    await closePolicy();
    draft = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    assert.deepEqual(draft.text_body.modes, ["text2img"]);
    assert.equal(draft.steps.modes, undefined, "saving a hidden selector does not add a mode restriction");
    await capability("img2img", true);
    await pick("text2img");
    await frame.locator('[data-edit-schema-policy="text_body"]').click();
    assert.equal(await frame.locator("#schemaPolicy-modes").inputValue(), "text2img", "both capabilities restore the selector with its original value");
    await closePolicy("取消");
    const policySave = page.waitForResponse(item => item.url().endsWith("/settings/save") && item.request().method() === "POST");
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await policySave).status(), 200);
    const policySettings = await (await page.request.get(`${api}/settings/get`)).json();
    const policies = policySettings.webui.providers.find(item => item.id === providerId).models[0].parameters;
    assert.deepEqual(policies.edit_body.modes, ["img2img"]);
    assert.equal(policies.edit_body.record_in_history, false);
    assert.deepEqual(policies.text_body.modes, ["text2img"]);
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: mode defaults, applicability, tool overrides and persistence passed`);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1280, 390]) await run(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
