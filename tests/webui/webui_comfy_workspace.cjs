/* Isolated UI contracts; generation and provider calls stay mocked. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-comfy-workspace-"));
const graph = { "1": { class_type: "SaveImage", inputs: { filename_prefix: "test" } } };
const workflow = { api_graph: graph, api_graph_json: JSON.stringify(graph), bindings: {}, outputs: ["1"], execution_policy: "fixed_outputs_v1" };
function model(provider, id, image = false) {
  return { id, name: `${provider.id}-${id}`, model_ref: `${provider.id}:${id}`, provider_id: provider.id, provider_kind: provider.kind, provider_name: provider.name, supports_text2img: !image, supports_img2img: image, supports_negative_prompt: false, max_reference_images: image ? 2 : 1, parameters: { steps: { type: "number", label: "步数", default: 20, request_key: "steps" }, ...(provider.kind === "comfyui" ? { count: { type: "integer", label: "生图张数", default: id === "beta" ? 6 : 1, min: 1, max: 16, request_key: "count", refill_from_history: false, webui_visible: id !== "beta" } } : {}) }, tool: { enabled: true, parameters: {} }, ...(provider.kind === "comfyui" ? { comfyui: workflow, comfyui_capabilities: { count_bound: false, prompt_required: false } } : {}) };
}
async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((select, next) => { select.value = next; select.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}
async function run(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 960 }, hasTouch: width < 600 });
  const page = await context.newPage();
  const providers = [
    { id: "ca", name: "本地 Comfy", kind: "comfyui", enabled: true },
    { id: "cb", name: "远端 Comfy", kind: "comfyui", enabled: true },
    { id: "normal", name: "普通服务商", kind: "openai_images", enabled: true },
  ];
  providers[0].models = [model(providers[0], "alpha"), model(providers[0], "beta"), model(providers[0], "edit", true)];
  providers[1].models = [model(providers[1], "alpha")];
  providers[2].models = [model(providers[2], "one"), model(providers[2], "two")];
  let replayDraft;
  const requests = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.route("**/studio/bootstrap", async route => {
      const response = await route.fetch(), payload = await response.json();
      await route.fulfill({ response, json: { ...payload, providers, models: providers.flatMap(item => item.models), defaults: { ...payload.defaults, text2img_model_ref: "ca:alpha", img2img_model_ref: "ca:edit" } } });
    });
    await page.route("**/settings/get", async route => {
      const response = await route.fetch(), payload = await response.json();
      payload.webui.providers = structuredClone(providers);
      await route.fulfill({ response, json: payload });
    });
    await page.route("**/comfy/jobs**", async route => {
      if (route.request().method() === "POST") { requests.push(route.request().postDataJSON()); await route.fulfill({ json: { job: { id: `job-${requests.length}`, status: "queued", model_name: "test" } } }); }
      else await route.fulfill({ json: { jobs: [], job: { id: "job-1", status: "running" } } });
    });
    await page.route("**/studio/parameters/resolve", route => route.fulfill({ json: { draft: replayDraft, requires_model_selection: false, warnings: [], unmapped: {} } }));
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    const options = await frame.locator("#modelChoice option").evaluateAll(items => items.map(item => item.value));
    assert.deepEqual(options, ["", "@comfy:ca", "@comfy:cb", "normal:one", "normal:two"], "top selector lists each Comfy provider once and preserves ordinary model entries");
    assert.equal(await frame.locator("#modelChoice").inputValue(), "@comfy:ca");
    assert.equal(await frame.locator("#comfyWorkflowChoice").inputValue(), "ca:alpha", "configured workflow defaults initialize both selectors");
    assert.deepEqual(await frame.locator("#comfyWorkflowChoice option").evaluateAll(items => items.map(item => item.value)), ["", "ca:alpha", "ca:beta"]);
    await choose(frame, "#modelChoice", "normal:one");
    assert.equal(await frame.locator("#comfyWorkflowWorkspace").isVisible(), false);
    await frame.locator('[data-model-parameter="steps"]').fill("33");
    await choose(frame, "#modelChoice", "normal:two");
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "33", "ordinary providers keep the existing model-switch parameter carry");
    await choose(frame, "#modelChoice", "@comfy:cb");
    assert.equal(await frame.locator("#comfyWorkflowWorkspace").isVisible(), true);
    assert.equal(await frame.locator("#generatorWorkspace").isVisible(), false);
    assert.equal(await frame.locator("#comfyWorkflowChoice").isDisabled(), false, "workflow choice remains usable before the form has a model");
    assert.equal(await frame.locator("#workspaceEmpty").textContent(), "请选择工作流");
    await choose(frame, "#comfyWorkflowChoice", "cb:alpha");
    assert.equal(await frame.locator("#generatorWorkspace").isVisible(), true);
    await choose(frame, "#modelChoice", "@comfy:ca");
    await choose(frame, "#comfyWorkflowChoice", "ca:alpha");
    await frame.locator('[data-model-parameter="steps"]').fill("42");
    await choose(frame, "#comfyWorkflowChoice", "ca:beta");
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "42", "common fields survive workflow changes");
    assert.equal(await frame.locator('[data-model-parameter="count"]').count(), 0, "count visibility follows its schema policy");
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => !document.getElementById("generateButton").disabled);
    assert.equal(requests[0].provider_id, "ca");
    assert.equal(requests[0].model_ref, "ca:beta");
    assert.equal(requests[0].model, "beta");
    assert.equal(requests[0].parameters.steps, 42);
    assert.equal(requests[0].count, 6, "hidden count uses this workflow's configured total default");
    await frame.locator('[data-mode="img2img"]').click();
    assert.equal(await frame.locator("#modelChoice").inputValue(), "@comfy:ca");
    assert.equal(await frame.locator("#comfyWorkflowChoice").inputValue(), "ca:edit");
    assert.deepEqual(await frame.locator("#comfyWorkflowChoice option").evaluateAll(items => items.map(item => item.value)), ["", "ca:edit"]);
    assert.equal(await frame.locator("#referenceField").isVisible(), true);
    // A historical text workflow remains selectable when its current template
    // now requires references. Both selector levels follow the historical draft.
    replayDraft = { mode: "text2img", provider_id: "ca", model: "edit", model_ref: "ca:edit", prompt: "old prompt", parameters: { steps: 17 }, notice: "已恢复参数，部分参数未能映射，请检查工作流。".repeat(8) + "long_unmapped_parameter_".repeat(12), comfyui: workflow, comfyui_model: { ...providers[0].models[2], supports_text2img: true, supports_img2img: false, max_reference_images: 1 } };
    await frame.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { readText: async () => "historical-workflow" } }));
    await frame.locator("#pasteParametersButton").click();
    await frame.waitForFunction(() => document.getElementById("comfyWorkflowChoice").value === "ca:edit" && document.querySelector('[data-model-parameter="steps"]')?.value === "17");
    assert.equal(await frame.locator("#modelChoice").inputValue(), "@comfy:ca");
    assert.equal(await frame.locator("#referenceField").isVisible(), false);
    assert.equal(await frame.locator("#generatorWorkspace").isVisible(), true);
    const actionLayout = await frame.locator("#generateButton").evaluate(button => {
      const range = document.createRange(); range.selectNodeContents(button);
      const text = range.getBoundingClientRect(), bounds = button.getBoundingClientRect();
      const notice = document.getElementById("generationError").getBoundingClientRect(), parent = button.parentElement.getBoundingClientRect();
      return { lines: new Set(Array.from(range.getClientRects(), rect => Math.round(rect.top))).size, textFits: text.left >= bounds.left && text.right <= bounds.right, contained: bounds.right <= parent.right + 1, separate: notice.right <= bounds.left || notice.bottom <= bounds.top, width: bounds.width };
    });
    assert.equal(actionLayout.lines, 1, "long notices never wrap the generate button label");
    assert.ok(actionLayout.textFits && actionLayout.contained && actionLayout.separate, JSON.stringify(actionLayout));
    await frame.locator(".form-actions").screenshot({ path: path.join(output, `${width}-long-notice.png`) });
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => !document.getElementById("generateButton").disabled);
    assert.deepEqual(requests.at(-1).comfyui, workflow);
    assert.equal(requests.at(-1).model, "edit");
    await page.screenshot({ path: path.join(output, `${width}-workflow-workspace.png`) });
    const geometry = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(geometry.scroll <= geometry.width + 1, JSON.stringify(geometry));
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="ca"]').click();
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator('[data-model-field="tool_selection_description"]').inputValue(), "");
    assert.equal(await frame.locator('[data-model-field="tool_prompt_instructions"]').inputValue(), "");
    assert.equal(await frame.locator('[data-model-field="tool_prompt_profile"]').inputValue(), "");
    assert.equal(await frame.locator('[data-model-field="tool_prompt_profile"] option:checked').textContent(), "未指定");
    await frame.locator('[data-settings-provider="normal"]').click();
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator('[data-model-field="tool_prompt_profile"]').inputValue(), "natural_language");
    assert.notEqual(await frame.locator('[data-model-field="tool_selection_description"]').inputValue(), "");
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}
(async () => {
  const browser = await playwright[process.env.STUDIO_BROWSER || "chromium"].launch({ headless: true });
  try { for (const width of [1440, 390]) await run(browser, width); }
  finally { await browser.close(); }
  console.log(`ComfyUI workspace selection passed; screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
