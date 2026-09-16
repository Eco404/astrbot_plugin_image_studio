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
const definition = { api_graph: graph, api_graph_json: JSON.stringify(graph).replace('"seed":42', '"seed":1152921504606847099'), bindings: {}, outputs: ["4"], execution_policy: "fixed_outputs_v1" };
const countSchema = () => ({ type: "integer", label: "生图张数", request_key: "count", default: 1, min: 1, max: 16, refill_from_history: false });
const makeModel = () => ({ id: "fixed", name: "固定工作流", supports_text2img: true, supports_img2img: false, supports_negative_prompt: false, max_reference_images: 1, native_batch_size: 1, max_concurrent_requests: 8, capability_source: "workflow", comfyui: structuredClone(definition), parameters: { count: countSchema() }, comfyui_capabilities: { prompt_required: false, count_bound: false }, tool: { enabled: true, max_reference_images: 1, parameters: {} } });
const makeCountModel = () => {
  const model = makeModel();
  Object.assign(model, { id: "counted", name: "多轮工作流", native_batch_size: 3, parameters: { count: { ...countSchema(), default: 7, max: 40 }, batch_size: { type: "number", label: "节点批次", default: 3, request_key: "batch_size" } }, comfyui_capabilities: { prompt_required: false, count_bound: false } });
  model.comfyui.bindings.batch_size = { type: "number", source: "parameter", targets: [{ node_id: "5", input_name: "batch_size" }] };
  model.parameters.seed_value = { type: "number", label: "种子", request_key: "seed_value", default: "1152921504606847099" };
  model.comfyui.bindings.seed_value = { type: "number", source: "seed", targets: [{ node_id: "3", input_name: "seed" }] };
  model.comfyui.api_graph["5"] = { class_type: "EmptyLatentImage", inputs: { width: 64, height: 64, batch_size: 3 } };
  model.comfyui.api_graph_json = JSON.stringify(model.comfyui.api_graph);
  return model;
};
const makeLegacyModel = () => {
  const model = makeCountModel();
  model.id = "legacy"; model.name = "旧版批次工作流";
  delete model.comfyui.execution_policy;
  model.comfyui.bindings = { count: { type: "number", source: "count", targets: [{ node_id: "5", input_name: "batch_size" }] } };
  model.parameters = { count: { type: "number", label: "旧节点批次", request_key: "count", default: 3 } };
  model.tool.parameters = { count: { exposed: true, default_override: 6 } };
  return model;
};
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=";
const result = { images: [{ data_url: png }, { data_url: png }], provider_name: "Comfy 测试", model: "fixed", elapsed_ms: 250, generation_id: "mock-result" };
async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((element, next) => { element.value = next; element.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}
async function addInput(frame, name = "自定义") {
  const before = await frame.locator("[data-comfy-binding]").count();
  await frame.locator('button[data-select-id="comfyAddBinding"]').click();
  const menu = frame.locator('.studio-select-menu[data-select-id="comfyAddBinding"]');
  assert.equal(await menu.getByRole("option").first().textContent(), "自定义");
  const layout = await menu.evaluate(element => {
    const bounds = element.getBoundingClientRect(), dialog = document.getElementById("studioModal").getBoundingClientRect();
    const label = element.querySelector(".studio-select-option-label").getBoundingClientRect();
    const trigger = document.querySelector('button[data-select-id="comfyAddBinding"]').getBoundingClientRect();
    return { width: bounds.width, left: bounds.left, right: bounds.right, dialogLeft: dialog.left, dialogRight: dialog.right, triggerRight: trigger.right, inset: label.left - bounds.left, mark: getComputedStyle(element.querySelector(".studio-select-mark")).display, options: element.querySelectorAll('[role="option"]').length, viewport: document.documentElement.clientWidth };
  });
  assert.equal(layout.mark, "none", "an add-action menu has no unused checkmark column");
  assert.ok(layout.inset <= 18, JSON.stringify(layout));
  assert.ok(layout.width <= 561 && layout.left >= layout.dialogLeft + 11 && layout.right <= layout.dialogRight - 11, JSON.stringify(layout));
  assert.ok(Math.abs(layout.right - layout.triggerRight) <= 10, "wide menu expands inward, with room for the dialog-edge clamp");
  if (layout.options > 2) assert.ok(layout.width > 220, "workflow names use content width instead of the narrow button width");
  await menu.screenshot({ path: path.join(output, `${layout.viewport}-add-input-menu.png`), animations: "disabled" });
  await menu.getByRole("option", { name, exact: true }).click();
  await frame.waitForFunction(count => document.querySelectorAll("[data-comfy-binding]").length === count + 1, before);
}
async function openJobs(frame) {
  await frame.locator("#comfyJobs").waitFor();
  if (!await frame.locator("#comfyJobs").evaluate(element => element.open)) await frame.locator("#comfyJobs > summary").click();
}
async function dropFiles(frame, selector, files) {
  return frame.evaluate(({ selector, files }) => {
    const target = document.querySelector(selector), modal = document.getElementById("studioModal");
    const dataTransfer = new DataTransfer();
    for (const file of files) dataTransfer.items.add(new File([file.base64 ? Uint8Array.from(atob(file.base64), char => char.charCodeAt(0)) : file.content], file.name, { type: file.type }));
    const enter = new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer });
    target.dispatchEvent(enter);
    const highlighted = modal.classList.contains("is-comfy-drop-target");
    const over = new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer });
    target.dispatchEvent(over);
    const drop = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer });
    target.dispatchEvent(drop);
    return { highlighted, accepted: over.defaultPrevented && drop.defaultPrevented, cleared: !modal.classList.contains("is-comfy-drop-target") };
  }, { selector, files });
}
async function matrix(browser, width, dark) {
  const context = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600, colorScheme: dark ? "dark" : "light" });
  const page = await context.newPage();
  const errors = [], submitted = [], inspections = [], saves = [], fileImports = [], dismissed = new Set();
  let rejectFileImport = false;
  let settings, remoteJob = null, polls = 0, showProvider = true, settingsRevision = 0;
  const provider = { id: "comfy-test", name: "Comfy 测试", kind: "comfyui", enabled: true, base_url: "http://comfy.invalid:8188", api_key: "", proxy: "", custom_headers: "", timeout_seconds: 600, max_concurrent_generations: 1, models: [makeModel(), makeCountModel(), makeLegacyModel()] };
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
      if (route.request().headers()["content-type"]?.includes("multipart/form-data")) {
        fileImports.push(route.request().postDataBuffer().toString());
        if (rejectFileImport) { await route.fulfill({ status: 400, json: { message: "图片未包含可执行的 ComfyUI API 工作流" } }); return; }
        // WebKit's request log omits uploaded file bytes. Let the original
        // upload reach the real importer and verify the decoded node below.
        if (fileImports.at(-1).includes('filename="workflow.json"')) { await route.continue(); return; }
      } else { const body = route.request().postDataJSON(); assert.ok(body.content || body.comfyui); await route.continue(); return; }
      await route.fulfill({ json: { comfyui: structuredClone(definition), parameters: {}, suggestions: {}, outputs: ["4"], capabilities: { prompt_required: false, count_bound: false } } });
    });
    await page.route("**/comfy/inspect", async route => {
      const body = route.request().postDataJSON(); inspections.push(body);
      const value = body.comfyui.api_graph["1"].inputs.ckpt_name;
      await route.fulfill({ json: { status: "blocked", issues: value === "missing.safetensors" ? [{ severity: "error", node_id: "1", input_name: "ckpt_name", message: "模型缺失：missing.safetensors" }] : [], models: [{ node_id: "1", input_name: "ckpt_name", options: ["available.safetensors"] }] } });
    });
    await page.route("**/comfy/jobs**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/dismiss")) { dismissed.add(route.request().postDataJSON().id); await route.fulfill({ json: { success: true } }); return; }
      if (url.pathname.endsWith("/cancel")) { remoteJob = { ...remoteJob, status: "cancelled" }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (url.pathname.endsWith("/resume")) { remoteJob = { ...remoteJob, status: "running", can_resume: false }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (route.request().method() === "POST") { submitted.push(route.request().postDataJSON()); polls = 0; remoteJob = { id: "job-1", model: "fixed", status: "queued", created_at: 1, progress: { completed: 0, total: 3 } }; await route.fulfill({ json: { job: remoteJob } }); return; }
      if (url.searchParams.has("id")) { polls++; remoteJob = { ...remoteJob, status: remoteJob.status === "partial" ? "partial" : polls > 1 ? "succeeded" : "running", ...(polls > 1 ? { result, result_available: true, progress: { completed: 3, total: 3 } } : {}) }; await route.fulfill({ json: { job: remoteJob } }); return; }
      const publicJob = remoteJob ? structuredClone(remoteJob) : null;
      if (publicJob) delete publicJob.result;
      await route.fulfill({ json: { jobs: publicJob && !dismissed.has(publicJob.id) ? [publicJob] : [] } });
    });
    await page.route("**/settings/save", async route => { const body = route.request().postDataJSON(); saves.push(body); provider.models = structuredClone(body.studio.providers.find(item => item.id === provider.id).models); settingsRevision++; await route.fulfill({ json: { settings_revision: settingsRevision } }); });
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1);
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#prompt").isVisible(), false, "fixed workflow needs no prompt");
    assert.equal(await frame.locator('[data-model-parameter="count"]').inputValue(), "1", "fixed workflows expose the independent total target without a node binding");
    await choose(frame, "#comfyWorkflowChoice", `${provider.id}:counted`);
    assert.equal(await frame.locator('[data-model-parameter="batch_size"]').inputValue(), "3");
    assert.equal(await frame.locator('[data-model-parameter="count"]').getAttribute("max"), "40", "existing schema limits are preserved");
    await frame.locator('[data-model-parameter="count"]').fill("5");
    await frame.locator("#generateButton").click();
    await frame.locator("#comfyJobs").filter({ hasText: "排队中" }).waitFor();
    assert.equal(await frame.locator("#comfyJobs").evaluate(element => element.open), false, "task queue starts collapsed");
    assert.match(await frame.locator("#comfyJobsSummary").textContent(), /进行中 1/);
    assert.equal(await frame.locator("[data-job-dismiss]").count(), 0, "running tasks cannot be dismissed");
    assert.match(await frame.locator("#comfyJobs").textContent(), /分批 0\/3/);
    assert.equal(submitted.length, 1); assert.equal(submitted[0].prompt, "");
    assert.equal(submitted[0].count, 5, "total target is sent at request level");
    assert.equal(submitted[0].parameters.batch_size, 3, "node batch stays an independent ordinary parameter");
    assert.equal(submitted[0].parameters.count, undefined);
    assert.equal(provider.models[1].comfyui.api_graph["5"].inputs.batch_size, 3);
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator("#comfyJobs").filter({ hasText: "已完成" }).waitFor({ timeout: 15000 });
    assert.equal(submitted.length, 1, "refresh recovers the existing job without resubmission");
    assert.equal(await frame.locator("#resultGrid .result-card").count(), 2);
    await openJobs(frame);
    await frame.locator("[data-job-result]").click();
    await frame.locator("#imagePreview").waitFor();
    assert.equal(submitted.length, 1, "a completed task remains viewable in the current page");
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#comfyJobs").isVisible(), false, "successful tasks disappear after a page reload");
    remoteJob = { ...remoteJob, status: "unknown", remote_id: null, can_resume: true, result_available: false, result: null };
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await openJobs(frame);
    assert.match(await frame.locator("#comfyJobsSummary").textContent(), /异常 1/);
    await frame.locator("[data-job-resume]").waitFor();
    assert.equal(await frame.locator("[data-job-result]").count(), 0, "unavailable parent results do not expose a result button");
    await frame.locator("[data-job-resume]").click();
    await frame.locator("#comfyJobs").filter({ hasText: "已完成" }).waitFor();
    assert.equal(await frame.locator("#comfyJobs").evaluate(element => element.open), true, "progress updates preserve expanded queue state");
    assert.equal(submitted.length, 1, "continue query never submits generation again");
    showProvider = false;
    remoteJob = { ...remoteJob, status: "partial", result_available: true, error: "第 2 批失败" };
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await openJobs(frame);
    assert.match(await frame.locator("#comfyJobsSummary").textContent(), /异常 1/);
    await frame.locator("[data-job-result]").waitFor();
    assert.equal(await frame.locator("#generatorWorkspace").isVisible(), false, "no active model leaves the generation form hidden");
    await frame.locator("[data-job-result]").click();
    await frame.locator("#imagePreview").waitFor();
    assert.equal(await frame.locator("#previewImage").getAttribute("src"), png, "saved images remain viewable without the provider");
    assert.equal(submitted.length, 1);
    await frame.locator("#closeImagePreview").click();
    await frame.locator("#imagePreview").waitFor({ state: "hidden" });
    await frame.locator("[data-job-dismiss]").click();
    await frame.locator("#comfyJobs").waitFor({ state: "hidden" });
    assert.equal(dismissed.has("job-1"), true, "failed or partial tasks can be persistently dismissed");
    showProvider = true;
    await page.reload();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator("#comfyJobs").isVisible(), false, "dismissed problems do not reappear after reload");
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="comfy-test"]').click();
    assert.equal(await frame.locator("#discoverModelsButton").count(), 0);
    assert.equal(await frame.locator('[data-provider-field="request_template"]').count(), 0);
    assert.equal(await frame.locator("#addModelButton").textContent(), "新增工作流");
    await frame.locator('[data-settings-model="counted"]').click();
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "3", "configured images per workflow run remain editable without count bindings");
    assert.match(await frame.locator('[data-model-field="native_batch_size"]').locator("..").textContent(), /工作流单次出图张数/);
    const totalDefault = JSON.parse(await frame.locator("#modelParametersSchema").inputValue()).count;
    assert.equal(totalDefault.default, 7); assert.equal(totalDefault.max, 40);
    assert.equal(totalDefault.refill_from_history, false);
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
    await frame.locator("#comfyEditWorkflow").click();
    await frame.locator("#comfyApplyWorkflow").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    const editedTotal = JSON.parse(await frame.locator("#modelParametersSchema").inputValue()).count;
    assert.equal(editedTotal.default, 7); assert.equal(editedTotal.max, 40);
    assert.equal(editedTotal.refill_from_history, false, "editing bindings preserves the separate total-count schema");
    await frame.locator('[data-settings-model="fixed"]').click();
    assert.equal(await frame.locator('[data-model-field="native_batch_size"]').inputValue(), "1", "all workflows expose images per run");
    await frame.locator("#comfyEditWorkflow").click();
    assert.match(await frame.locator("#studioModalTitle").textContent(), /ComfyUI/);
    await frame.locator("#comfyInspect").click();
    await frame.locator("#comfyCompatibility").filter({ hasText: "模型缺失" }).waitFor();
    assert.equal(inspections.length, 1);
    await frame.locator("#comfyFixedInputs").evaluate(element => { for (const details of [element.parentElement, ...element.querySelectorAll("details")]) details.open = true; });
    const singleFields = await frame.locator("#comfyFixedInputs .comfy-binding-fields > .field:only-child").evaluateAll(fields => fields.map(field => ({ field: field.getBoundingClientRect().width, row: field.parentElement.getBoundingClientRect().width })));
    assert.ok(singleFields.length > 0);
    assert.ok(singleFields.every(size => Math.abs(size.field - size.row) < 1), "a fixed node's only input spans the entire row");
    assert.equal(await frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]').inputValue(), "1152921504606847099", "64-bit seeds keep exact decimal digits");
    assert.match(inspections[0].comfyui.api_graph_json, /1152921504606847099/);
    await choose(frame, '[data-fixed-node="1"][data-fixed-input="ckpt_name"]', "available.safetensors");
    await addInput(frame);
    assert.equal(await frame.locator('[data-binding-field="source"] option[value="count"]').count(), 0, "total count cannot be wired into a node");
    for (const key of ["count", "n"]) {
      await frame.locator('[data-binding-field="key"]').fill(key);
      await frame.locator("#comfyApplyWorkflow").click();
      await frame.locator("#studioModalError").filter({ hasText: "保留给本次总张数" }).waitFor();
    }
    await frame.locator('[data-binding-field="key"]').fill("scene");
    await frame.locator('[data-binding-field="label"]').fill("画面内容");
    await choose(frame, '[data-binding-field="source"]', "prompt");
    await frame.locator("[data-binding-targets]").evaluate(select => { for (const option of select.options) option.selected = option.value === JSON.stringify(["2", "text"]); select.dispatchEvent(new Event("change", { bubbles: true })); });
    assert.equal(await frame.locator('[data-binding-targets] option').evaluateAll(options => options.some(option => option.textContent.includes("positive"))), false, "linked conditioning is not an editable literal");
    await addInput(frame, "#3 · KSampler → seed");
    await addInput(frame, "#3 · KSampler → steps");
    assert.equal(await frame.locator("[data-comfy-binding]").count(), 3, "different fields on the same node can be exposed independently");
    assert.equal(await frame.locator('#comfyAddBinding option').filter({ hasText: "#3 · KSampler → seed" }).isDisabled(), true, "already bound candidates cannot be added twice");
    await addInput(frame);
    const duplicate = frame.locator("[data-comfy-binding]").last();
    const unavailableSeed = duplicate.locator('[data-binding-targets] option').filter({ hasText: "#3 · KSampler → seed" });
    // isDisabled follows this enclosing label to its enabled SELECT; inspect
    // the OPTION itself and verify the actual custom menu's disabled state.
    assert.equal(await unavailableSeed.evaluate(option => option.disabled), true, "custom inputs also exclude targets owned by another input");
    await duplicate.locator('.comfy-targets .studio-select-trigger').click();
    const unavailableOption = frame.locator('.studio-select-menu[role="group"] [role="option"]').filter({ hasText: "#3 · KSampler → seed" });
    assert.equal(await unavailableOption.getAttribute("aria-disabled"), "true");
    await unavailableOption.click({ force: true });
    assert.equal(await duplicate.locator('[data-binding-targets]').evaluate(select => select.selectedOptions.length), 0, "clicking an occupied field cannot select it");
    await frame.locator('.studio-select-menu[role="group"]').press("Escape");
    // A stale or externally edited draft still receives an explicit error at apply.
    await duplicate.locator("[data-binding-targets]").evaluate(select => { const item = Array.from(select.options).find(option => option.value === JSON.stringify(["3", "seed"])); item.selected = true; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await frame.locator("#studioModalFooter button").filter({ hasText: "应用工作流" }).click();
    await frame.locator("#studioModalError").filter({ hasText: "seed 已由" }).waitFor();
    await frame.locator("[data-binding-remove]").last().click();
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
    assert.equal(schema.count.default, 1); assert.equal(schema.count.max, 16); assert.equal(schema.count.refill_from_history, false);
    assert.equal(schema.scene, undefined, "main prompt is separate from parameter controls");
    assert.ok(schema.node_3_seed && schema.node_3_steps, "distinct inputs on the same node can be applied successfully");
    await frame.locator("#comfyEditWorkflow").click();
    assert.equal(await frame.locator('[data-binding-field="source"]').first().inputValue(), "prompt");
    assert.equal(await frame.locator("[data-comfy-binding]").count(), 3, "editing a saved model preserves its explicitly configured inputs");
    assert.equal(await frame.locator('[data-fixed-node="1"][data-fixed-input="ckpt_name"]').inputValue(), "available.safetensors");
    await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator('[data-settings-model="legacy"]').click();
    await frame.locator("#comfyEditWorkflow").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "visible" });
    const migratedKey = await frame.locator('[data-binding-field="key"]').inputValue();
    assert.match(migratedKey, /^workflow_count/);
    assert.equal(await frame.locator('[data-binding-field="source"]').inputValue(), "parameter");
    assert.equal(await frame.locator('[data-fixed-node="5"][data-fixed-input="batch_size"]').inputValue(), "3");
    await frame.locator("#comfyApplyWorkflow").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    const migratedSchema = JSON.parse(await frame.locator("#modelParametersSchema").inputValue());
    assert.equal(migratedSchema.count.default, 1, "legacy node values never become the new total target default");
    assert.equal(migratedSchema[migratedKey].default, 3);
    assert.equal(migratedSchema[migratedKey].request_key, migratedKey);
    await frame.locator('[data-model-tab="tool"]').click();
    await frame.locator(`[data-edit-tool-parameter="${migratedKey}"]`).click();
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "6", "legacy tool defaults follow the renamed ordinary binding");
    await frame.locator("#parameterDialogCancel").click();
    await frame.locator("#parameterDialog").waitFor({ state: "hidden" });
    await frame.locator('[data-model-tab="model"]').click();
    await frame.locator("#newModelChoice").fill("imported-workflow");
    await frame.locator("#addModelButton").click();
    assert.equal(await frame.locator("#comfyWorkflowEditing").isVisible(), false, "empty graphs show only import controls");
    assert.equal(await frame.locator("#comfyApplyWorkflow").isDisabled(), true);
    await frame.locator("#comfyImportJSON").fill("not a workflow");
    await frame.locator("#comfyReadJSON").click();
    await frame.locator("#comfyImportStatus").filter({ hasNotText: "正在读取" }).waitFor();
    assert.equal(await frame.locator("#comfyWorkflowEditing").isVisible(), false, "failed imports leave the empty editor hidden");
    assert.equal(await frame.locator("#comfyApplyWorkflow").isDisabled(), true);
    await frame.locator("#comfyImportJSON").fill(JSON.stringify(graph));
    await frame.locator("#comfyReadJSON").click();
    await frame.locator("#comfyImportStatus").filter({ hasText: "已读取" }).waitFor();
    assert.equal(await frame.locator("#comfyOutputs").inputValue(), "4");
    assert.equal(await frame.locator("#comfyWorkflowEditing").isVisible(), true);
    assert.equal(await frame.locator("#comfyApplyWorkflow").isDisabled(), false, "valid graphs can be applied even with zero exposed inputs");
    assert.equal(await frame.locator("[data-comfy-binding]").count(), 0, "pasting API JSON leaves inputs empty");
    assert.ok(await frame.locator('#comfyAddBinding option').count() > 2, "identified inputs remain available in the add menu");
    const gaps = await frame.evaluate(() => {
      const rect = id => document.getElementById(id).getBoundingClientRect();
      return { fileToJSON: document.getElementById("comfyImportJSON").closest("label").getBoundingClientRect().top - rect("comfyChooseFile").bottom, jsonToRead: rect("comfyReadJSON").top - rect("comfyImportJSON").bottom };
    });
    assert.ok(gaps.fileToJSON >= 12 && gaps.jsonToRead >= 12, JSON.stringify(gaps));
    await frame.locator(".comfy-import").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${width}-import-spacing.png`) });
    const imageFile = { name: "workflow.png", type: "image/png", base64: png.split(",")[1] };
    const jsonFile = { name: "workflow.json", type: "application/json", content: JSON.stringify(graph) };
    for (const [target, file] of [["#studioModal > header", imageFile], ["#comfyNodeCount", jsonFile], ["#studioModalFooter", jsonFile]]) {
      await frame.locator(".comfy-import").evaluate(element => { element.open = false; });
      const before = fileImports.length;
      assert.deepEqual(await dropFiles(frame, target, [file]), { highlighted: true, accepted: true, cleared: true });
      await frame.locator("#comfyImportStatus").filter({ hasText: "已读取" }).waitFor();
      assert.equal(fileImports.length, before + 1, "header, body and footer share exactly one import handler");
      assert.ok(fileImports.at(-1).includes(`filename="${file.name}"`));
      if (file.content) assert.equal(await frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]').inputValue(), "42", "the real importer receives and decodes the dropped JSON file");
      assert.equal(await frame.locator(".comfy-import").evaluate(element => element.open), true, "drop reveals import feedback even when collapsed");
      assert.equal(await frame.locator("#comfyOutputs").inputValue(), "4");
      assert.equal(await frame.locator("[data-comfy-binding]").count(), 0, "dropping an image or JSON never automatically adds bindings");
    }
    const beforeMultiple = fileImports.length;
    await dropFiles(frame, "#studioModalFooter", [imageFile, jsonFile]);
    assert.equal(fileImports.length, beforeMultiple, "multiple workflows are not silently merged or ignored");
    assert.equal(await frame.locator("#comfyNodeCount").textContent(), "4 个节点");
    rejectFileImport = true;
    await dropFiles(frame, "#studioModal > header", [imageFile]);
    await frame.locator("#comfyImportStatus").filter({ hasText: "未包含" }).waitFor();
    assert.equal(await frame.locator("#comfyNodeCount").textContent(), "4 个节点", "invalid image preserves the current draft");
    rejectFileImport = false;
    await frame.locator("#studioModalFooter button").filter({ hasText: "应用工作流" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal(await frame.locator('[data-settings-model="imported-workflow"]').count(), 1);
    await frame.locator("#comfyEditWorkflow").click();
    const beforeReopen = fileImports.length;
    await dropFiles(frame, "#studioModalFooter", [jsonFile]);
    await frame.locator("#comfyImportStatus").filter({ hasText: "已读取" }).waitFor();
    assert.equal(fileImports.length, beforeReopen + 1, "reopening removes the old modal drag handlers");
    await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}
(async () => {
  const browser = await playwright[process.env.STUDIO_BROWSER || "chromium"].launch({ headless: true });
  try { for (const [width, dark] of [[1440, false], [720, true], [390, false]]) await matrix(browser, width, dark); }
  finally { await browser.close(); }
  console.log(`ComfyUI provider UI passed; screenshots: ${output}`);
})().catch(error => { console.error(error); process.exit(1); });
