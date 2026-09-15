/* ComfyUI connections and tasks are mocked; use an isolated WebUI harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-comfy-provider-"));
const graph = {
  "1": { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: "missing.safetensors" } },
  "2": { class_type: "CLIPTextEncode", inputs: { clip: ["1", 1], text: "a quiet mountain lake" } },
  "3": { class_type: "KSampler", inputs: { positive: ["2", 0], seed: 42, steps: 20 } },
  "4": { class_type: "SaveImage", inputs: { images: ["3", 0], filename_prefix: "audit" } },
};
const definition = { api_graph: graph, api_graph_json: JSON.stringify(graph).replace('"seed":42', '"seed":1152921504606847099'), bindings: {}, outputs: ["4"] };
const makeModel = () => ({ id: "fixed", name: "固定工作流", supports_text2img: true, supports_img2img: false, supports_negative_prompt: false, max_reference_images: 1, native_batch_size: 1, max_concurrent_requests: 8, capability_source: "workflow", comfyui: structuredClone(definition), parameters: {}, comfyui_capabilities: { prompt_required: false, count_bound: false }, tool: { enabled: true, max_reference_images: 1, parameters: {} } });
const makeCountModel = () => {
  const model = makeModel();
  Object.assign(model, { id: "counted", name: "分批工作流", native_batch_size: 3, parameters: { batch_size: { type: "number", label: "张数", default: 1, request_key: "count" } }, comfyui_capabilities: { prompt_required: false, count_bound: true } });
  model.comfyui.bindings.batch_size = { type: "number", source: "count", targets: [{ node_id: "5", input_name: "batch_size" }] };
  model.parameters.seed_value = { type: "number", label: "种子", request_key: "seed_value", default: "1152921504606847099" };
  model.comfyui.bindings.seed_value = { type: "number", source: "seed", targets: [{ node_id: "3", input_name: "seed" }] };
  model.comfyui.api_graph["5"] = { class_type: "EmptyLatentImage", inputs: { width: 64, height: 64, batch_size: 1 } };
  model.comfyui.api_graph_json = JSON.stringify(model.comfyui.api_graph);
  return model;
};
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=";
const result = { images: [{ data_url: png }, { data_url: png }], provider_name: "Comfy 测试", model: "fixed", elapsed_ms: 250, generation_id: "mock-result" };
async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((element, next) => { element.value = next; element.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}
async function matrix(browser, width, dark) {
  const context = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600, colorScheme: dark ? "dark" : "light" });
  const page = await context.newPage();
  const errors = [], submitted = [], inspections = [], saves = [];
  let settings, remoteJob = null, polls = 0, showProvider = true, settingsRevision = 0;
  const provider = { id: "comfy-test", name: "Comfy 测试", kind: "comfyui", enabled: true, base_url: "http://comfy.invalid:8188", api_key: "", proxy: "", custom_headers: "", timeout_seconds: 600, max_concurrent_generations: 1, models: [makeModel(), makeCountModel()] };
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.route("**/settings/get", async route => {
      const response = await route.fetch(); settings = await response.json();
      settings.webui.providers = settings.webui.providers.filter(item => item.id !== provider.id); settings.webui.providers.push(structuredClone(provider));
      settings.webui.revision = settingsRevision; settings.webui.ui = { ...(settings.webui.ui || {}), settings_revision: settingsRevision };
      await route.fulfill({ response, json: settings });
    });
    await page.route("**/studio/bootstrap", async route => {
      const response = await route.fetch(), payload = await response.json();
      if (showProvider) {
        payload.providers.push(structuredClone(provider));
        payload.models.push(...provider.models.map(model => ({ ...structuredClone(model), provider_id: provider.id, provider_kind: "comfyui", provider_name: provider.name, model_ref: `${provider.id}:${model.id}` })));
      }
      payload.defaults.text2img_model_ref = showProvider ? `${provider.id}:fixed` : "";
      await route.fulfill({ response, json: payload });
    });
    await page.route("**/comfy/import", async route => {
      const body = route.request().postDataJSON(); assert.ok(body.content);
      await route.fulfill({ json: { comfyui: structuredClone(definition), parameters: {}, suggestions: {}, outputs: ["4"], capabilities: { prompt_required: false, count_bound: false } } });
    });
    await page.route("**/comfy/inspect", async route => {
      const body = route.request().postDataJSON(); inspections.push(body);
      const value = body.comfyui.api_graph["1"].inputs.ckpt_name;
      await route.fulfill({ json: { status: "blocked", issues: value === "missing.safetensors" ? [{ severity: "error", node_id: "1", input_name: "ckpt_name", message: "模型缺失：missing.safetensors" }] : [], models: [{ node_id: "1", input_name: "ckpt_name", options: ["available.safetensors"] }] } });
    });
    await page.route("**/comfy/jobs**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/cancel")) { remoteJob = { ...remoteJob, status: "cancelled" }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (url.pathname.endsWith("/resume")) { remoteJob = { ...remoteJob, status: "running", can_resume: false }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (route.request().method() === "POST") { submitted.push(route.request().postDataJSON()); polls = 0; remoteJob = { id: "job-1", model: "fixed", status: "queued", created_at: 1, progress: { completed: 0, total: 3 } }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (url.searchParams.has("id")) { polls++; remoteJob = { ...remoteJob, status: polls > 1 ? "succeeded" : "running", ...(polls > 1 ? { result, result_available: true, progress: { completed: 3, total: 3 } } : {}) }; await route.fulfill({ json: { job: remoteJob } }); return; }
      const publicJob = remoteJob ? structuredClone(remoteJob) : null;
      if (publicJob) delete publicJob.result;
      await route.fulfill({ json: { jobs: publicJob ? [publicJob] : [] } });
    });
    await page.route("**/settings/save", async route => { const body = route.request().postDataJSON(); saves.push(body); provider.models = structuredClone(body.studio.providers.find(item => item.id === provider.id).models); settingsRevision++; await route.fulfill({ json: { settings_revision: settingsRevision } }); });
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1);
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#prompt").isVisible(), false, "fixed workflow needs no prompt");
    assert.equal(await frame.locator('[data-model-parameter="count"]').count(), 0, "unbound output count stays workflow-defined");
    await frame.locator("#generateButton").click();
    await frame.locator("#comfyJobs").filter({ hasText: "排队中" }).waitFor();
    assert.match(await frame.locator("#comfyJobs").textContent(), /分批 0\/3/);
    assert.equal(submitted.length, 1); assert.equal(submitted[0].prompt, "");
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator("#comfyJobs").filter({ hasText: "已完成" }).waitFor({ timeout: 15000 });
    assert.equal(submitted.length, 1, "refresh recovers the existing job without resubmission");
    assert.equal(await frame.locator("#resultGrid .result-card").count(), 2);
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("[data-job-result]").waitFor();
    assert.equal(await frame.locator("#resultGrid .result-card").count(), 0, "result_available restores a lightweight entry without image data");
    assert.equal(await frame.locator("[data-job-resume]").count(), 0, "completed jobs have no resume action");
    await frame.locator("[data-job-result]").click();
    await frame.locator("#resultGrid .result-card").nth(1).waitFor();
    await frame.locator("#imagePreview").waitFor();
    assert.equal(submitted.length, 1, "viewing a restored result only fetches existing images");
    remoteJob = { ...remoteJob, status: "unknown", remote_id: null, can_resume: true, result_available: false, result: null };
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("[data-job-resume]").waitFor();
    assert.equal(await frame.locator("[data-job-result]").count(), 0, "unavailable parent results do not expose a result button");
    await frame.locator("[data-job-resume]").click();
    await frame.locator("#comfyJobs").filter({ hasText: "已完成" }).waitFor();
    assert.equal(submitted.length, 1, "continue query never submits generation again");
    showProvider = false;
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("[data-job-result]").waitFor();
    assert.equal(await frame.locator("#generatorWorkspace").isVisible(), false, "no active model leaves the generation form hidden");
    await frame.locator("[data-job-result]").click();
    await frame.locator("#imagePreview").waitFor();
    assert.equal(await frame.locator("#previewImage").getAttribute("src"), png, "saved images remain viewable without the provider");
    assert.equal(submitted.length, 1);
    showProvider = true;
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="comfy-test"]').click();
    assert.equal(await frame.locator("#discoverModelsButton").count(), 0);
    assert.equal(await frame.locator('[data-provider-field="request_template"]').count(), 0);
    assert.equal(await frame.locator("#addModelButton").textContent(), "新增工作流");
    await frame.locator('[data-settings-model="counted"]').click();
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "3", "persisted count capability keeps batch limit editable");
    assert.equal(await frame.locator('[data-model-field="max_concurrent_requests"]').getAttribute("max"), "16");
    await frame.locator('[data-model-tab="tool"]').click();
    await frame.locator('[data-edit-tool-parameter="seed_value"]').click();
    await frame.locator("#toolParameterDefault").fill("18446744073709551615");
    await frame.locator("#parameterDialogApply").click();
    await frame.locator("#parameterDialog").waitFor({ state: "hidden" });
    await frame.locator("#saveSettingsButton").click();
    await frame.waitForFunction(() => !document.getElementById("saveSettingsButton").disabled);
    assert.ok(saves.length > 0);
    const savedSeed = saves.at(-1).studio.providers.find(item => item.id === provider.id).models.find(model => model.id === "counted").tool.parameters.seed_value.default_override;
    assert.equal(savedSeed, "18446744073709551615", "uint64 tool overrides are sent as exact decimal strings");
    await frame.locator('[data-settings-model="counted"]').click();
    await frame.locator('[data-model-tab="tool"]').click();
    await frame.locator('[data-edit-tool-parameter="seed_value"]').click();
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "18446744073709551615", "saving and rereading retains every uint64 digit");
    await frame.locator("#parameterDialogCancel").click();
    await frame.locator("#parameterDialog").waitFor({ state: "hidden" });
    await frame.locator('[data-model-tab="model"]').click();
    await frame.locator('[data-settings-model="fixed"]').click();
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').count(), 0, "unbound workflows expose no misleading native batch limit");
    await frame.locator("#comfyEditWorkflow").click();
    assert.match(await frame.locator("#studioModalTitle").textContent(), /ComfyUI/);
    await frame.locator("#comfyInspect").click();
    await frame.locator("#comfyCompatibility").filter({ hasText: "模型缺失" }).waitFor();
    assert.equal(inspections.length, 1);
    await frame.locator("#comfyFixedInputs").evaluate(element => { for (const details of [element.parentElement, ...element.querySelectorAll("details")]) details.open = true; });
    assert.equal(await frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]').inputValue(), "1152921504606847099", "64-bit seeds keep exact decimal digits");
    assert.match(inspections[0].comfyui.api_graph_json, /1152921504606847099/);
    await choose(frame, '[data-fixed-node="1"][data-fixed-input="ckpt_name"]', "available.safetensors");
    await frame.locator("#comfyAddBinding").click();
    await frame.locator('[data-binding-field="key"]').fill("scene");
    await frame.locator('[data-binding-field="label"]').fill("画面内容");
    await choose(frame, '[data-binding-field="source"]', "prompt");
    await frame.locator("[data-binding-targets]").evaluate(select => { for (const option of select.options) option.selected = option.value === JSON.stringify(["2", "text"]); select.dispatchEvent(new Event("change", { bubbles: true })); });
    assert.equal(await frame.locator('[data-binding-targets] option').evaluateAll(options => options.some(option => option.textContent.includes("positive"))), false, "linked conditioning is not an editable literal");
    const geometry = await frame.evaluate(() => ({ viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth, modal: document.getElementById("studioModal").getBoundingClientRect().width }));
    assert.ok(geometry.page <= geometry.viewport + 1, JSON.stringify(geometry));
    assert.ok(geometry.modal <= geometry.viewport, JSON.stringify(geometry));
    await frame.waitForFunction(() => ["#studioModal", ".studio-modal-scrim"].every(selector => document.querySelector(selector).getAnimations().every(animation => !["running", "pending"].includes(animation.playState))));
    const materials = await frame.evaluate(async () => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const surface = selector => { const style = getComputedStyle(document.querySelector(selector)); return { background: style.backgroundColor, filter: style.backdropFilter || style.webkitBackdropFilter, opacity: style.opacity }; };
      return { modal: surface("#studioModal"), detail: surface("#detailDrawer"), scrim: surface(".studio-modal-scrim"), detailScrim: surface("#scrim"), footer: surface("#studioModalFooter"), detailFooter: surface("#detailFooter") };
    });
    assert.equal(materials.modal.background, materials.detail.background, "workflow modal reuses detail glass opacity and tint");
    assert.equal(materials.modal.filter, materials.detail.filter, "workflow modal uses exactly the detail backdrop blur");
    assert.match(materials.modal.filter, /blur\(24px\)/);
    assert.deepEqual(materials.scrim, materials.detailScrim, "workflow modal reuses the shared scrim");
    assert.equal(materials.footer.background, materials.detailFooter.background, "footer uses the same single-layer tint");
    assert.equal(materials.footer.filter, "none");
    fs.writeFileSync(path.join(output, `${width}-${dark ? "dark" : "light"}-material.json`), JSON.stringify(materials, null, 2));
    await page.screenshot({ path: path.join(output, `${width}-${dark ? "dark" : "light"}-editor.png`) });
    await frame.locator("#studioModalFooter button").filter({ hasText: "应用工作流" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    const schema = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    assert.equal(schema.scene, undefined, "main prompt is separate from parameter controls");
    await frame.locator("#comfyEditWorkflow").click();
    assert.equal(await frame.locator('[data-binding-field="source"]').inputValue(), "prompt");
    assert.equal(await frame.locator('[data-fixed-node="1"][data-fixed-input="ckpt_name"]').inputValue(), "available.safetensors");
    await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator("#newModelChoice").fill("imported-workflow");
    await frame.locator("#addModelButton").click();
    await frame.locator("#comfyImportJSON").fill(JSON.stringify(graph));
    await frame.locator("#comfyReadJSON").click();
    await frame.locator("#comfyImportStatus").filter({ hasText: "已读取" }).waitFor();
    assert.equal(await frame.locator("#comfyOutputs").inputValue(), "4");
    await frame.locator("#studioModalFooter button").filter({ hasText: "应用工作流" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal(await frame.locator('[data-settings-model="imported-workflow"]').count(), 1);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}
(async () => {
  const browser = await playwright[process.env.STUDIO_BROWSER || "chromium"].launch({ headless: true });
  try { for (const [width, dark] of [[1440, false], [720, true], [390, false]]) await matrix(browser, width, dark); }
  finally { await browser.close(); }
  console.log(`ComfyUI provider UI passed; screenshots: ${output}`);
})().catch(error => { console.error(error); process.exit(1); });
