/* Run only against tests.webui_harness: no real providers or deployment data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-parameter-policy-"));

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
  const key = `${engine}-${width}-${Date.now()}`;
  const providerId = `policy-${key}`;
  const modelId = `batch-policy-${key}`;
  const modelRef = `${providerId}:${modelId}`;
  let frame;
  const schema = {
    samples: { type: "integer", default: 1, min: 1, max: 4, request_key: "n", refill_from_history: false },
    steps: { type: "integer", default: 24, min: 1, max: 30 },
    hidden: { type: "integer", default: 7, request_key: "hidden_api", webui_visible: false },
    visibility: { type: "integer", default: 3, min: 0, max: 100 },
    ephemeral: { type: "text", default: "default text", record_in_history: false },
    no_refill: { type: "text", default: "current text", refill_from_history: false },
    enabled: { type: "boolean", default: true },
    style: { type: "preset", default: "sample", ui_only: true, target: "artist", record_in_history: false, choices: [{ value: "sample", label: "Sample", fill: "sample artist" }, { value: "custom", label: "Custom", fill: "" }] },
    artist: { type: "text", default: "" },
    optional_choice: { type: "select", choices: ["alpha", "beta"] },
    optional_number: { type: "integer", min: 1, max: 20 },
  };
  async function settings() { return (await page.request.get(`${apiRoot}/settings/get`)).json(); }
  async function save(value) {
    const response = await page.request.post(`${apiRoot}/settings/save`, { data: { base: value.base, studio: value.webui, settings_revision: value.webui.revision } });
    assert.equal(response.status(), 200, await response.text());
  }
  async function reload() {
    await page.goto(base);
    await page.locator("#studio").waitFor();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
  }
  async function edit() {
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${providerId}"]`).click();
    await frame.locator(`[data-settings-model="${modelId}"]`).click();
    await frame.locator('[data-model-tab="model"]').click();
  }
  async function paste(parameters) {
    const content = JSON.stringify({ format: "image_studio", version: 1, data: { mode: "text2img", model_ref: modelRef, provider_id: providerId, model: modelId, prompt: "Clipboard fixture", count: 3, parameters } });
    await frame.evaluate(text => { Object.defineProperty(navigator, "clipboard", { configurable: true, value: { readText: async () => text } }); }, content);
    const request = page.waitForRequest(item => new URL(item.url()).pathname.endsWith("/studio/parameters/resolve"));
    await frame.locator("#pasteParametersButton").click();
    assert.equal((await request).postDataJSON().for_reproduction, false);
    await frame.locator("#appNoticeMessage").filter({ hasText: "参数已填入" }).waitFor();
  }
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/, "isolated harness required");
    const initial = await settings();
    initial.webui.history.max_records = 0;
    initial.webui.history.max_megabytes = 0;
    initial.webui.providers.push({ id: providerId, name: "Parameter policy fixture", kind: "custom_json", enabled: true, base_url: "https://example.test", models: [{ id: modelId, name: "Parameter policy", supports_text2img: true, supports_img2img: false, parameters: schema }] });
    initial.webui.generation_defaults.page.text2img_model_ref = modelRef;
    await save(initial);
    await reload();
    assert.equal(await frame.locator('[data-model-parameter="hidden"]').count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="artist"]').inputValue(), "sample artist", "fresh initialization expands model preset defaults");
    assert.equal(await frame.locator('[data-model-parameter="style"]').inputValue(), "sample");
    assert.equal(await frame.locator('[data-model-parameter="optional_choice"]').inputValue(), "", "a select without default stays unset");
    await edit();
    assert.equal(await frame.locator('[data-schema-default="hidden"]').count(), 1, "hidden generation controls remain editable in settings");
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    for (const name of ["webui_visible", "record_in_history", "refill_from_history"]) assert.equal(await frame.locator(`#schemaPolicy-${name}`).isChecked(), true, "omitted flags default true");
    await frame.locator("#studioModalRoot .studio-modal-scrim").click({ position: { x: 2, y: 2 }, force: true });
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true, "outside click does not close parameter policy dialog");
    await frame.locator("#schemaPolicy-refill_from_history").uncheck({ force: true });
    await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    assert.equal(await frame.locator("#schemaPolicy-refill_from_history").isChecked(), true, "cancel discards unsaved policy");
    const visible = frame.locator("#schemaPolicy-webui_visible");
    const recorded = frame.locator("#schemaPolicy-record_in_history");
    const refill = frame.locator("#schemaPolicy-refill_from_history");
    await visible.uncheck({ force: true });
    assert.equal(await refill.isChecked(), false, "hiding a parameter clears refill");
    assert.equal(await refill.isDisabled(), false, "refill remains actionable for reverse dependency activation");
    assert.equal(await recorded.isChecked(), true, "hidden parameters can still be recorded");
    await frame.locator('label[for="schemaPolicy-refill_from_history"]').click();
    assert.equal(await refill.isChecked(), true);
    assert.equal(await visible.isChecked(), true, "enabling refill enables visibility");
    assert.equal(await recorded.isChecked(), true);
    await visible.uncheck({ force: true });
    await recorded.uncheck({ force: true });
    await refill.check({ force: true });
    assert.equal(await visible.isChecked(), true, "enabling refill restores both missing prerequisites");
    assert.equal(await recorded.isChecked(), true);
    await refill.uncheck({ force: true });
    assert.equal(await visible.isChecked(), true, "disabling refill leaves prerequisites untouched");
    assert.equal(await recorded.isChecked(), true);
    await visible.uncheck({ force: true });
    await recorded.uncheck({ force: true });
    await visible.check({ force: true });
    assert.equal(await refill.isChecked(), false, "enabling a prerequisite does not enable refill");
    await recorded.check({ force: true });
    assert.equal(await refill.isDisabled(), false);
    assert.equal(await refill.isChecked(), false, "restoring prerequisites does not silently enable refill");
    await refill.check({ force: true });
    await recorded.uncheck({ force: true });
    assert.equal(await refill.isChecked(), false, "disabling recording clears refill");
    assert.equal(await refill.isDisabled(), false);
    assert.equal(await visible.isChecked(), true, "recording is independent from visibility");
    await refill.check({ force: true });
    assert.equal(await recorded.isChecked(), true, "enabling refill enables recording");
    await recorded.uncheck({ force: true });
    await recorded.check({ force: true });
    await frame.locator("#studioModalFooter button").filter({ hasText: "保存" }).click();
    assert.equal(JSON.parse(await frame.locator("#modelParametersSchema").inputValue()).steps.refill_from_history, false, "save persists dependent switch clearing");
    await frame.locator('[data-edit-schema-policy="steps"]').click();
    assert.equal(await refill.isChecked(), false, "reopening retains the saved off state");
    await refill.check({ force: true });
    await page.screenshot({ path: path.join(output, `${engine}-${width}-policy-dialog.png`) });
    await frame.locator("#studioModalFooter button").filter({ hasText: "保存" }).click();
    const edited = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    assert.equal(edited.steps.webui_visible, true);
    assert.equal(edited.steps.record_in_history, true);
    assert.equal(edited.steps.refill_from_history, true);
    for (const name of ["hidden", "style"]) {
      await frame.locator(`[data-edit-schema-policy="${name}"]`).click();
      assert.equal(await refill.isChecked(), false, "initial incompatible refill is cleared in the dialog");
      assert.equal(await refill.isDisabled(), false);
      await page.screenshot({ path: path.join(output, `${engine}-${width}-policy-${name}-linked.png`) });
      await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
      assert.equal(Object.hasOwn(JSON.parse(await frame.locator("#modelParametersSchema").inputValue())[name], "refill_from_history"), false, "cancel does not normalize the original schema");
    }
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator('[data-edit-tool-parameter="hidden"]').count(), 1, "tool exposure is independent from WebUI visibility");
    const saved = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/settings/save"));
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await saved).status(), 200);
    await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
    await frame.locator('[data-view="generate"]').click();
    await choose(frame, "#modelChoice", modelRef);
    await frame.locator('[data-model-parameter="samples"]').fill("2");
    await frame.locator('[data-model-parameter="steps"]').fill("28");
    await frame.locator('[data-model-parameter="visibility"]').fill("88");
    await frame.locator('[data-model-parameter="ephemeral"]').fill("do not record this");
    await frame.locator('[data-model-parameter="no_refill"]').fill("historical text");
    await frame.locator('[data-model-parameter="enabled"]').uncheck({ force: true });
    await frame.locator('[data-model-parameter="artist"]').fill("");
    assert.equal(await frame.locator('[data-model-parameter="style"]').inputValue(), "custom", "explicit empty artist wins");
    await frame.locator("#prompt").fill("Policy regression fixture");
    const generated = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/studio/generate"));
    await frame.locator("#generateButton").click();
    const generation = await generated;
    assert.equal(generation.status(), 200, await generation.text());
    const result = await generation.json();
    assert.equal(result.images.length, 2);
    const detail = await (await page.request.get(`${apiRoot}/gallery/detail/${result.generation_id}`)).json();
    assert.equal(Object.hasOwn(detail.parameters.parameters, "ephemeral"), false);
    assert.equal(detail.parameters.parameters.steps, 28);
    assert.equal(detail.parameters.parameters.hidden_api, 7);
    assert.equal(detail.parameters.parameters.artist, "");
    assert.equal(Object.hasOwn(detail.parameters.parameters, "optional_choice"), false);
    assert.equal(Object.hasOwn(detail.parameters.parameters, "optional_number"), false);
    const next = await settings();
    const model = next.webui.providers.find(item => item.id === providerId).models[0];
    model.parameters.samples.default = 1;
    model.parameters.hidden.default = 9;
    model.parameters.visibility.default = 5;
    model.parameters.visibility.webui_visible = false;
    model.parameters.ephemeral.default = "new default text";
    model.parameters.no_refill.default = "new current text";
    await save(next);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(`.gallery-card[data-gallery-id="${result.generation_id}"]`).click();
    await frame.locator("#detailReproduce").click();
    await frame.locator("#generateView.is-active").waitFor();
    assert.equal(await frame.locator('[data-model-parameter="samples"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "28");
    assert.equal(await frame.locator('[data-model-parameter="ephemeral"]').inputValue(), "new default text");
    assert.equal(await frame.locator('[data-model-parameter="no_refill"]').inputValue(), "new current text");
    assert.equal(await frame.locator('[data-model-parameter="visibility"]').count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="enabled"]').isChecked(), false);
    assert.equal(await frame.locator('[data-model-parameter="artist"]').inputValue(), "");
    assert.equal(await frame.locator('[data-model-parameter="style"]').inputValue(), "custom");
    await paste({ steps: 20, hidden_api: 99, visibility: 99, no_refill: "explicit pasted value", style: "sample", artist: "" });
    assert.equal(await frame.locator('[data-model-parameter="samples"]').inputValue(), "3", "clipboard paste is not reproduction");
    assert.equal(await frame.locator('[data-model-parameter="no_refill"]').inputValue(), "explicit pasted value");
    assert.equal(await frame.locator('[data-model-parameter="artist"]').inputValue(), "");
    assert.equal(await frame.locator('[data-model-parameter="style"]').inputValue(), "custom");
    const hiddenSettings = await settings();
    const hiddenModel = hiddenSettings.webui.providers.find(item => item.id === providerId).models[0];
    hiddenModel.parameters.artist.webui_visible = false;
    hiddenModel.parameters.style.record_in_history = true;
    await save(hiddenSettings);
    await reload();
    assert.equal(await frame.locator('[data-model-parameter="artist"]').count(), 0);
    await choose(frame, '[data-model-parameter="style"]', "custom");
    await choose(frame, '[data-model-parameter="style"]', "sample");
    await frame.locator("#prompt").fill("Hidden preset target fixture");
    const hiddenRequest = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/studio/generate"));
    const hiddenResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/studio/generate"));
    await frame.locator("#generateButton").click();
    const hiddenPayload = (await hiddenRequest).postDataJSON();
    assert.equal(hiddenPayload.parameters.artist, "sample artist", "manual preset updates target even without target DOM control");
    assert.equal(hiddenPayload.parameters.style, "sample", "UI-only preset is sent to backend for optional history recording");
    const hiddenGenerated = await hiddenResponse;
    assert.equal(hiddenGenerated.status(), 200);
    const hiddenResult = await hiddenGenerated.json();
    const hiddenDetail = await (await page.request.get(`${apiRoot}/gallery/detail/${hiddenResult.generation_id}`)).json();
    assert.equal(hiddenDetail.parameters.parameters.style, "sample", "record-enabled UI-only field preserves actual selected value");
    const hiddenStyle = await settings();
    const styleModel = hiddenStyle.webui.providers.find(item => item.id === providerId).models[0];
    styleModel.parameters.artist.webui_visible = true;
    styleModel.parameters.style.webui_visible = false;
    await save(hiddenStyle);
    await reload();
    assert.equal(await frame.locator('[data-model-parameter="style"]').count(), 0);
    await paste({ artist: "explicit visible artist", style: "sample" });
    assert.equal(await frame.locator('[data-model-parameter="artist"]').inputValue(), "explicit visible artist");
    await page.screenshot({ path: path.join(output, `${engine}-${width}-generation.png`) });
    const referenceSettings = await settings();
    const referenceModel = referenceSettings.webui.providers.find(item => item.id === providerId).models[0];
    referenceModel.supports_img2img = true;
    referenceModel.max_reference_images = 2;
    referenceModel.capability_source = "manual";
    await save(referenceSettings);
    const referenceBytes = Buffer.from(result.images[0].data_url.split(",")[1], "base64");
    const uploadedResponse = await page.request.post(`${apiRoot}/studio/reference/upload`, { multipart: { file: { name: "reference.png", mimeType: "image/png", buffer: referenceBytes } } });
    assert.equal(uploadedResponse.status(), 200, await uploadedResponse.text());
    const uploaded = await uploadedResponse.json();
    const editingResponse = await page.request.post(`${apiRoot}/studio/generate`, { data: { mode: "img2img", model_ref: modelRef, prompt: "Retained reference fixture", reference_ids: [uploaded.id] } });
    assert.equal(editingResponse.status(), 200, await editingResponse.text());
    const editing = await editingResponse.json();
    const replacementSettings = await settings();
    const replacementModel = replacementSettings.webui.providers.find(item => item.id === providerId).models[0];
    replacementModel.id = `${modelId}-replacement`;
    const replacementRef = `${providerId}:${replacementModel.id}`;
    replacementSettings.webui.generation_defaults.page.text2img_model_ref = replacementRef;
    replacementSettings.webui.generation_defaults.page.img2img_model_ref = replacementRef;
    await save(replacementSettings);
    await reload();
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(`.gallery-card[data-gallery-id="${editing.generation_id}"]`).click();
    const resolutions = [];
    const captureResolution = request => { if (new URL(request.url()).pathname.endsWith("/studio/parameters/resolve")) resolutions.push(request.postDataJSON()); };
    page.on("request", captureResolution);
    await frame.locator("#detailReproduce").click();
    await frame.locator("#parameterTargetModel").waitFor({ state: "attached" });
    await choose(frame, "#parameterTargetModel", replacementRef);
    await frame.locator("#studioModalFooter button").filter({ hasText: "填入参数" }).click();
    await frame.locator("#generateView.is-active").waitFor();
    page.off("request", captureResolution);
    assert.equal(resolutions.length, 2, "reproduction resolves parameters again after target selection");
    assert.ok(resolutions.every(request => request.for_reproduction === true), "target selection preserves reproduction policy mode");
    assert.equal(await frame.locator("#referenceStrip .reference-item").count(), 1, "retained references survive replacement-model selection");
    assert.doesNotMatch(await frame.locator("#parameterImportNotice").textContent(), /参数文本不包含原始参考图/);
    const bounds = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(bounds.scroll <= bounds.width + 1, `page overflow: ${JSON.stringify(bounds)}`);
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: schema controls, current policy reproduction, clipboard and hidden presets passed`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const width of [1440, 390]) await matrix(browser, width);
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
