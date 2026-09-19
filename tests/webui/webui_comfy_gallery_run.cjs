/* Real import, model normalization and settings APIs; remote Comfy calls mocked. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-comfy-gallery-run-"));
const lockedSeed = 234754882058856;
const graph = {
  "1": { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: "missing.safetensors" } },
  "2": { class_type: "CLIPTextEncode", inputs: { clip: ["1", 1], text: "a quiet mountain lake" } },
  "3": { class_type: "KSampler", inputs: { model: ["1", 0], positive: ["2", 0], latent_image: ["7", 0], seed: lockedSeed, steps: 20 } },
  "4": { class_type: "SaveImage", inputs: { images: ["9", 0], filename_prefix: "gallery-test" } },
  "6": { class_type: "LoadImage", inputs: { image: "previous-reference.png" } },
  "7": { class_type: "VAEEncode", inputs: { pixels: ["6", 0], vae: ["1", 2] } },
  "9": { class_type: "KSampler", inputs: { model: ["1", 0], positive: ["2", 0], latent_image: ["3", 0], seed: 43, steps: 5 } },
};
const raw = { api_graph: graph, api_graph_json: JSON.stringify(graph), bindings: {}, outputs: ["4"], execution_policy: "fixed_outputs_v1" };
const historical = structuredClone(raw);
historical.api_graph["1"].inputs.ckpt_name = "available.safetensors";
historical.api_graph_json = JSON.stringify(historical.api_graph);
historical.bindings = {
  prompt: { source: "prompt", type: "text", targets: [{ node_id: "2", input_name: "text" }] },
  seed: { source: "seed", type: "number", targets: [{ node_id: "3", input_name: "seed" }, { node_id: "9", input_name: "seed" }] },
  reference: { source: "reference", type: "image", reference_index: 0, targets: [{ node_id: "6", input_name: "image" }] },
};
const schema = { count: { type: "integer", label: "本次张数", default: 7, min: 1, max: 40, refill_from_history: false }, seed: { type: "integer", label: "历史种子标签", default: 42, min: -1, max: "18446744073709551615", history_record: true }, note: { type: "text", label: "保留的隐藏项", default: "unchanged", webui_visible: false, history_record: false } };
const model = { id: "saved", name: "已保存的工作流", comfyui: historical, parameters: schema, native_batch_size: 1, max_concurrent_requests: 8, supports_img2img: true, supports_text2img: false, max_reference_images: 1 };
async function api(page, method, path, data) {
  const response = await page.request[method](`${base}/astrbot_plugin_image_studio/${path}`, data === undefined ? {} : { data });
  const payload = await response.json();
  assert.ok(response.ok(), `${path}: ${JSON.stringify(payload)}`); return payload;
}
async function choose(frame, selector, value) { await frame.locator(selector).evaluate((select, value) => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); }, value); }
async function addInput(frame, name) {
  await frame.locator('button[data-select-id="comfyAddBinding"]').click();
  await frame.locator('.studio-select-menu[data-select-id="comfyAddBinding"]').getByRole("option", { name, exact: true }).click();
}
async function assertSeedNotice(frame, selector, expectedCount = 2) {
  const notice = frame.locator(`${selector} .comfy-seed-notice`);
  await notice.waitFor();
  assert.equal(await notice.locator("strong").first().textContent(), "种子设置提示");
  assert.equal(await notice.locator("button:not(.comfy-seed-link)").count(), 0, "seed guidance has no dismiss button");
  assert.equal(await notice.locator(".comfy-seed-link").count(), selector === "#comfySeedWarnings" ? expectedCount : 0, "editor warnings link to their fixed inputs");
  const rows = await notice.locator("li").allTextContents();
  assert.equal(rows.length, expectedCount, "only unresolved locked seeds have a row");
  assert.match(rows[0], new RegExp(`节点 #3 · seed = (42|${lockedSeed})\\s*种子已锁定`));
  if (expectedCount > 1) assert.match(rows[1], /节点 #9 · seed = (42|43)\s*种子已锁定/);
  assert.equal((await notice.textContent()).split("如需恢复随机").length - 1, 1, "shared guidance appears once even with multiple locked seeds");
}
async function followSeedWarning(frame, nodeId, keyboard = false) {
  const selector = `[data-fixed-node="${nodeId}"][data-fixed-input="seed"]`;
  const input = frame.locator(selector), value = await input.inputValue();
  await frame.locator("#comfyFixedInputs").evaluate(element => {
    element.closest("details").open = false;
    element.querySelectorAll("details").forEach(node => { node.open = false; });
  });
  const warning = frame.locator(`#comfySeedWarnings [data-seed-node="${nodeId}"][data-seed-input="seed"]`);
  if (keyboard) { await warning.focus(); await warning.press("Enter"); }
  else await warning.click();
  await frame.waitForFunction(selector => {
    const input = document.querySelector(selector), bounds = input.getBoundingClientRect(), body = document.getElementById("studioModalBody").getBoundingClientRect();
    return document.activeElement === input && bounds.top >= body.top && bounds.bottom <= body.bottom;
  }, selector);
  assert.equal(await input.evaluate(element => element.closest("details").open && document.getElementById("comfyFixedInputs").closest("details").open), true, "both fixed inputs and the exact seed node expand");
  assert.equal(await frame.locator("#comfyFixedInputs details[open]").count(), 1, "unrelated nodes remain collapsed");
  assert.equal(await input.inputValue(), value, "navigation keeps the seed value unchanged");
}
async function followCompatibilityIssue(frame, keyboard = false) {
  const list = frame.locator("#comfyCompatibility .comfy-issues");
  assert.equal(await list.locator("li").count(), 5);
  assert.equal(await list.locator("button").count(), 1, "only issues with an exact editable field become navigation links");
  await frame.locator("#comfyFixedInputs").evaluate(element => {
    element.closest("details").open = false;
    element.querySelectorAll("details").forEach(node => { node.open = false; });
  });
  const link = list.locator('[data-issue-node="1"][data-issue-input="ckpt_name"]');
  if (keyboard) { await link.focus(); await link.press("Enter"); }
  else await link.click();
  await frame.waitForFunction(() => {
    const input = document.querySelector('[data-fixed-node="1"][data-fixed-input="ckpt_name"]');
    const target = input.closest(".studio-select").querySelector(".studio-select-trigger");
    const bounds = target.getBoundingClientRect(), body = document.getElementById("studioModalBody").getBoundingClientRect();
    return document.activeElement === target && bounds.top >= body.top && bounds.bottom <= body.bottom;
  });
  const input = frame.locator('[data-fixed-node="1"][data-fixed-input="ckpt_name"]');
  assert.equal(await input.evaluate(element => element.closest("details").open && document.getElementById("comfyFixedInputs").closest("details").open), true);
  assert.equal(await frame.locator("#comfyFixedInputs details[open]").count(), 1, "only the problem node expands");
  assert.equal(await input.inputValue(), "missing.safetensors", "jumping to a dependency does not change its value");
}
async function captureThemes(page, frame, width, label, selector) {
  const original = await frame.evaluate(() => window.ImageStudioAppearance.get());
  try {
    for (const preference of ["light", "dark"]) {
      await frame.evaluate(preference => window.ImageStudioAppearance.set({ preference }, false), preference);
      await frame.locator(selector).scrollIntoViewIfNeeded();
      await frame.evaluate(async () => {
        window.__dismissImageStudioNotice?.();
        await Promise.race([
          Promise.all(document.getAnimations().filter(item => item.effect?.getTiming().iterations !== Infinity).map(item => item.finished.catch(() => {}))),
          new Promise(resolve => setTimeout(resolve, 1200)),
        ]);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      const geometry = await frame.evaluate(selector => {
        const host = document.querySelector(selector), rect = host.getBoundingClientRect();
        return { viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth, left: rect.left, right: rect.right, width: host.clientWidth, content: host.scrollWidth };
      }, selector);
      assert.ok(geometry.page <= geometry.viewport + 1 && geometry.left >= -1 && geometry.right <= geometry.viewport + 1 && geometry.content <= geometry.width + 1, `${preference} seed layout: ${JSON.stringify(geometry)}`);
      if (label === "editor-issues") {
        assert.equal(await frame.evaluate(() => {
          const seedColor = getComputedStyle(document.querySelector("#comfySeedWarnings li")).color;
          return Array.from(document.querySelectorAll("#comfyCompatibility li, #comfyCompatibility .comfy-issue-link")).every(element => getComputedStyle(element).color === seedColor);
        }), true, `${preference}: compatibility entries use the same warning color as seed reminders`);
      }
      await page.screenshot({ path: path.join(output, `${width}-${label}-${preference}.png`) });
    }
  } finally { await frame.evaluate(original => window.ImageStudioAppearance.set(original, false), original); }
}
async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 980 }, hasTouch: width < 600 });
  const errors = [], submitted = [], saves = [], automaticSaves = [], imports = [], inspections = [], reproductions = [];
  page.on("pageerror", error => errors.push(error.message));
  let available = true, rejectImport = false, currentJob = null, heldImport = null, releaseHeldImport = null, heldHealth = null, releaseHeldHealth = null;
  const provider = { id: "comfy-gallery-empty", name: "空的 ComfyUI", kind: "comfyui", enabled: true, base_url: "http://comfy.invalid:8188", models: [] };
  const savedProvider = { ...provider, id: "comfy-gallery-saved", name: "已配置的 ComfyUI", models: [structuredClone(model)] };
  const initial = await api(page, "get", "settings/get");
  initial.webui.providers = initial.webui.providers.filter(item => !item.id.startsWith("comfy-gallery-"));
  initial.webui.providers.push(provider, savedProvider);
  await api(page, "post", "settings/save", { settings_revision: initial.webui.revision, base: initial.base, studio: initial.webui });
  const cards = (await api(page, "get", "gallery/list?limit=24")).items.slice(0, 3);
  const specs = new Map(cards.map((card, index) => [card.id, { historical: index > 0, matched: index === 2 }]));
  const parsedRaw = await api(page, "post", "comfy/import", { comfyui: raw });
  const parsedHistorical = await api(page, "post", "comfy/import", { comfyui: historical, parameters: schema });
  await page.addInitScript(() => {
    let factory;
    Object.defineProperty(window, "ImageStudioLibrary", { configurable: true, get: () => factory, set(value) { factory = hooks => { window.__galleryState = hooks.state; return value(hooks); }; } });
    let settingsFactory;
    Object.defineProperty(window, "ImageStudioSettings", { configurable: true, get: () => settingsFactory, set(value) { settingsFactory = hooks => { const controller = value(hooks); window.__settingsController = controller; return controller; }; } });
  });
  function fixture(detail, image, spec) {
    Object.assign(detail, { source: spec.historical ? "webui" : "import", provider_kind: spec.historical ? "comfyui" : "import", provider_id: spec.matched ? savedProvider.id : "removed-provider", generation_engine: "comfyui", model: spec.matched ? "saved" : "removed-workflow" });
    if (image) { image.metadata = rejectImport ? {} : { format: "comfyui", normalized: { prompt: "old image" }, raw: { prompt: JSON.stringify(graph) }, warnings: [] }; image.supplemental = spec.historical ? { comfyui: historical } : {}; }
  }
  await page.route("**/studio/bootstrap", async route => {
    const response = await route.fetch(), payload = await response.json();
    if (!available) { payload.providers = payload.providers.filter(item => item.kind !== "comfyui"); payload.models = payload.models.filter(item => item.provider_kind !== "comfyui"); }
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/gallery/detail/**", async route => { const response = await route.fetch(), detail = await response.json(), spec = specs.get(detail.id); if (spec) { fixture(detail, null, spec); detail.images.forEach(image => fixture(detail, image, spec)); } await route.fulfill({ response, json: detail }); });
  await page.route("**/gallery/image-info/**", async route => { const response = await route.fetch(), payload = await response.json(), spec = specs.get(payload.detail_fields.id || payload.image.generation_id); if (spec) fixture(payload.detail_fields, payload.image, spec); await route.fulfill({ response, json: payload }); });
  await page.route("**/comfy/import", async route => {
    const body = route.request().postDataJSON(); imports.push(body);
    if (!body.generation_id) {
      if (heldImport && body.comfyui) {
        const held = heldImport; heldImport = null;
        const response = await route.fetch(); held.started();
        await held.gate;
        await route.fulfill({ response }); held.completed();
        return;
      }
      return route.continue();
    }
    if (rejectImport) return route.fulfill({ status: 400, json: { message: "图片仅包含界面工作流，缺少可执行的 ComfyUI API 图" } });
    const spec = specs.get(body.generation_id), parsed = structuredClone(spec.historical ? parsedHistorical : parsedRaw);
    await route.fulfill({ json: { ...parsed, historical_snapshot: spec.historical, matched_model_ref: spec.matched ? `${savedProvider.id}:saved` : "", providers: [provider, savedProvider].map(({ id, name }) => ({ id, name })), model: { ...structuredClone(model), native_batch_size: 1, max_concurrent_requests: 8, comfyui: parsed.comfyui, parameters: parsed.parameters }, references: [], warnings: spec.historical ? ["历史参考图未保留，请重新补充。"] : [] } });
  });
  await page.route("**/comfy/inspect", async route => {
    const body = route.request().postDataJSON(); inspections.push(body);
    const config = body.comfyui, value = config.input_overrides?.find(item => item.node_id === "1" && item.input_name === "ckpt_name")?.value ?? config.api_graph["1"].inputs.ckpt_name;
    await route.fulfill({ json: { compatible: value === "available.safetensors", issues: value === "available.safetensors" ? [] : [
      { severity: "error", node_id: "1", input_name: "ckpt_name", message: "模型缺失：missing.safetensors" },
      { severity: "warning", node_id: "missing", input_name: "value", message: "节点不存在" },
      { severity: "warning", node_id: "1", input_name: "missing_input", message: "输入不存在" },
      { severity: "warning", node_id: "2", input_name: "clip", message: "已连接端口无法通过固定值编辑" },
      { severity: "warning", message: "动态依赖以执行时校验为准" },
    ], models: [{ node_id: "1", input_name: "ckpt_name", options: ["available.safetensors"] }] } });
  });
  await page.route("**/comfy/jobs**", route => {
    if (route.request().method() === "POST") { submitted.push(route.request().postDataJSON()); currentJob = { id: "temporary-job", status: "running", model_name: "临时工作流", created_at: 1 }; return route.fulfill({ json: { job: currentJob } }); }
    return route.fulfill({ json: new URL(route.request().url()).searchParams.has("id") ? { job: currentJob } : { jobs: currentJob ? [currentJob] : [] } });
  });
  await page.route("**/comfy/workflows", route => { automaticSaves.push(route.request().postDataJSON()); return route.fulfill({ status: 500, json: { message: "禁止自动保存" } }); });
  await page.route("**/settings/save", route => { saves.push(route.request().postDataJSON()); return route.continue(); });
  await page.route("**/storage/health", async route => {
    const held = saves.length ? heldHealth : null;
    if (held) heldHealth = null;
    const response = await route.fetch();
    if (held) { held.started(); await held.gate; }
    await route.fulfill({ response });
  });
  await page.route("**/gallery/reproduce/**", route => { reproductions.push(route.request().url()); return route.fulfill({ json: { provider_id: savedProvider.id, model: "saved", model_ref: `${savedProvider.id}:saved`, mode: "img2img", prompt: "historical matched prompt", parameters: { seed: 42 }, comfyui: historical, comfyui_model: { ...model, provider_kind: "comfyui" }, references: [] } }); });
  let frame;
  async function openGallery(index = 0) {
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(`[data-gallery-id="${cards[index].id}"] .gallery-info`).click();
    await frame.waitForFunction(() => !document.getElementById("detailFooter").inert);
    await frame.locator("#detailReproduce").click();
  }
  try {
    await page.goto(base); frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.locator(`#modelChoice option[value="@comfy:${provider.id}"]`).count(), 1, "providers without workflows remain selectable");
    await openGallery();
    assert.equal(await frame.locator("#comfyGalleryUse").inputValue(), "temporary");
    await choose(frame, "#comfyGalleryProvider", provider.id);
    await frame.locator("#comfyGalleryContinue").click();
    await frame.locator("#comfyEditor").waitFor();
    assert.equal(await frame.locator("#comfyWorkflowName").count(), 0, "temporary workflows use an internal name");
    assert.equal(await frame.locator("[data-comfy-binding]").count(), 0, "raw images start with no configured bindings");
    assert.equal(await frame.locator("#comfyNativeBatch").inputValue(), "1"); assert.equal(await frame.locator("#comfyConcurrency").inputValue(), "8");
    await frame.locator("#comfySeedWarnings").filter({ hasText: String(lockedSeed) }).waitFor();
    await assertSeedNotice(frame, "#comfySeedWarnings");
    await captureThemes(page, frame, width, "editor-seed", "#comfySeedWarnings");
    await followSeedWarning(frame, "3");
    await followSeedWarning(frame, "9", true);
    await frame.locator("#comfyNativeBatch").fill("2"); await frame.locator("#comfyConcurrency").fill("3");
    await frame.locator("#comfyApplyWorkflow").click();
    await frame.locator("#studioModalError").filter({ hasText: "兼容性检查未通过" }).waitFor();
    assert.equal(submitted.length, 0); assert.equal(saves.length, 0);
    assert.equal(await frame.locator("#comfyEditor").isVisible(), true);
    await captureThemes(page, frame, width, "editor-issues", "#comfyCompatibility");
    await followCompatibilityIssue(frame);
    await frame.locator("#comfyFixedInputs").evaluate(element => { element.parentElement.open = true; element.querySelectorAll("details").forEach(node => { node.open = true; }); });
    const fixedSeed = frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]');
    const fixedSeed9 = frame.locator('[data-fixed-node="9"][data-fixed-input="seed"]');
    const seedRows = frame.locator("#comfySeedWarnings li");
    const seedRow3 = seedRows.filter({ hasText: "节点 #3" });
    const seedRow9 = seedRows.filter({ hasText: "节点 #9" });
    const gate = new Promise(resolve => { releaseHeldImport = resolve; });
    let markStarted, markCompleted;
    const started = new Promise(resolve => { markStarted = resolve; });
    const completed = new Promise(resolve => { markCompleted = resolve; });
    heldImport = { gate, started: markStarted, completed: markCompleted };
    await fixedSeed.fill("81");
    assert.match(await seedRow3.textContent(), /seed = 81\s*种子已锁定/, "typing updates the displayed value before blur or server response");
    assert.equal(await fixedSeed.evaluate(input => document.activeElement === input), true);
    await Promise.race([started, new Promise((_, reject) => setTimeout(() => reject(new Error("seed refresh did not start")), 10000))]);
    await fixedSeed.fill("-1");
    assert.equal(await seedRow3.count(), 0, "fixed -1 hides its warning immediately without blur");
    assert.equal(await seedRow9.count(), 1, "other nodes remain visible");
    await fixedSeed9.fill("-1");
    assert.equal(await frame.locator("#comfySeedWarnings .comfy-seed-notice").count(), 0, "the whole panel disappears when no unresolved seeds remain");
    releaseHeldImport();
    await completed;
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await frame.locator("#comfySeedWarnings .comfy-seed-notice").count(), 0, "a delayed old parse result cannot restore resolved warnings");
    await fixedSeed9.fill("43");
    assert.equal(await seedRow9.count(), 1, "changing back to a fixed seed restores its warning");
    await followSeedWarning(frame, "9");
    await frame.locator("#comfyFixedInputs").evaluate(element => { element.querySelectorAll("details").forEach(node => { node.open = true; }); });
    await fixedSeed.fill(String(lockedSeed));
    await assertSeedNotice(frame, "#comfySeedWarnings");
    await seedRow9.locator("button").click();
    assert.equal(await fixedSeed9.evaluate(input => document.activeElement === input), true, "blurring an edited seed does not replace the clicked warning before navigation");
    await addInput(frame, "#3 · KSampler → steps");
    await assertSeedNotice(frame, "#comfySeedWarnings");
    await addInput(frame, "#3 · KSampler → seed");
    assert.equal(await seedRow3.count(), 0, "adding the exact seed input resolves its reminder even with a positive default");
    assert.equal(await seedRow9.count(), 1);
    await choose(frame, '[data-comfy-binding="1"] [data-binding-field="source"]', "parameter");
    assert.equal(await seedRow3.count(), 0, "ordinary parameter bindings also resolve their exact seed input");
    await frame.locator("#comfyFixedInputs").evaluate(element => { element.parentElement.open = true; element.querySelectorAll("details").forEach(node => { node.open = true; }); });
    await fixedSeed9.fill("-1");
    assert.equal(await frame.locator("#comfySeedWarnings .comfy-seed-notice").count(), 0);
    await frame.locator('[data-binding-remove="1"]').click();
    assert.equal(await seedRow3.count(), 1, "removing the exact binding restores the fixed-seed reminder");
    assert.equal(await seedRow9.count(), 0);
    await frame.locator('[data-binding-remove="0"]').click();
    await frame.locator("#comfyFixedInputs").evaluate(element => { element.parentElement.open = true; element.querySelectorAll("details").forEach(node => { node.open = true; }); });
    await fixedSeed9.fill("");
    assert.equal(await fixedSeed9.inputValue(), "", "a seed may remain blank while editing");
    assert.equal(await seedRow9.count(), 0, "a blank seed is not shown as a fixed-seed warning");
    await fixedSeed9.blur();
    assert.equal(await fixedSeed9.inputValue(), "-1", "a blank seed becomes -1 when the input loses focus");
    await fixedSeed9.fill("43");
    await assertSeedNotice(frame, "#comfySeedWarnings");
    await fixedSeed9.fill("");
    await choose(frame, '[data-fixed-node="1"][data-fixed-input="ckpt_name"]', "available.safetensors");
    await frame.locator("#comfyApplyWorkflow").click();
    await frame.locator("#comfyTemporaryInfo").waitFor();
    const temporaryRef = await frame.locator("#comfyWorkflowChoice").inputValue();
    assert.match(temporaryRef, /^comfy-gallery-empty:temporary_[a-f0-9]{32}$/);
    assert.equal(await frame.locator('[data-model-parameter="count"]').inputValue(), "1");
    assert.equal(await frame.evaluate(() => window.__galleryState.models.some(item => item.temporary)), false, "temporary models are kept outside global configured models");
    assert.equal((await api(page, "get", "settings/get")).webui.providers.find(item => item.id === provider.id).models.length, 0);
    await assertSeedNotice(frame, "#comfyTemporaryInfo", 1);
    await captureThemes(page, frame, width, "temporary-seed", "#comfyTemporaryInfo");
    await choose(frame, "#modelChoice", "natural:studio-image");
    await choose(frame, "#modelChoice", `@comfy:${provider.id}`); await choose(frame, "#comfyWorkflowChoice", temporaryRef);
    await assertSeedNotice(frame, "#comfyTemporaryInfo", 1);
    await frame.locator("#generateButton").click();
    await frame.waitForFunction(() => !document.getElementById("generateButton").disabled);
    assert.equal(submitted.length, 1); assert.equal(submitted[0].provider_id, provider.id);
    assert.equal(submitted[0].temporary_model.name, "临时工作流");
    assert.equal(submitted[0].temporary_model.native_batch_size, 2); assert.equal(submitted[0].temporary_model.max_concurrent_requests, 3);
    assert.deepEqual(submitted[0].temporary_model.comfyui.bindings, {}); assert.equal(submitted[0].temporary_model.comfyui.api_graph["3"].inputs.seed, lockedSeed); assert.equal(submitted[0].temporary_model.comfyui.api_graph["9"].inputs.seed, -1, "blank seeds are normalized to -1 when saving");
    await page.screenshot({ path: path.join(output, `${width}-temporary.png`) });
    await page.reload(); frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    assert.equal(await frame.evaluate(() => window.__galleryState.comfyuiTemporaryModel), null);
    await frame.locator("#comfyJobs").waitFor(); assert.equal(submitted.length, 1, "refresh restores the task without resubmission");
    await openGallery(1); await frame.locator("#comfyGalleryContinue").click(); await frame.locator("#comfyEditor").waitFor();
    assert.equal(await frame.locator("[data-comfy-binding]").count(), 3, "orphaned historical workflows retain bindings");
    assert.equal(await frame.locator("#comfySeedWarnings .comfy-seed-notice").count(), 0, "historical exposed seeds need no configuration reminder");
    assert.deepEqual(await frame.locator("#comfyOutputs").evaluate(select => [...select.selectedOptions].map(item => item.value)), ["4"]);
    await frame.locator("#comfyApplyWorkflow").click(); await frame.locator("#comfyTemporaryInfo").waitFor();
    assert.equal(await frame.locator("#comfyTemporaryInfo .comfy-seed-notice").count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="count"]').inputValue(), "1");
    assert.equal(await frame.locator('[data-model-parameter="count"]').getAttribute("max"), "40");
    assert.equal(await frame.locator('[data-model-parameter="seed"]').inputValue(), "42");
    assert.equal(await frame.locator('[data-model-parameter="seed"]').getAttribute("min"), "-1");
    assert.equal(await frame.evaluate(() => window.__galleryState.comfyuiTemporaryModel.parameters.note.default), "unchanged");
    assert.equal(await frame.locator("#referenceField").isVisible(), true, "missing references may be added after preparing the workflow");
    const historicalRef = await frame.locator("#comfyWorkflowChoice").inputValue();
    await frame.locator("#comfyEditTemporary").click(); await frame.locator("#comfyEditor").waitFor();
    assert.equal(await frame.locator("#comfySeedWarnings .comfy-seed-notice").count(), 0);
    await frame.locator("#comfyFixedInputs").evaluate(element => { element.parentElement.open = true; element.querySelectorAll("details").forEach(node => { node.open = true; }); });
    await frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]').fill("-1");
    await frame.locator('[data-fixed-node="3"][data-fixed-input="seed"]').press("Tab");
    await frame.waitForFunction(() => !document.querySelector("#comfySeedWarnings .comfy-seed-notice"));
    await frame.locator("#comfyApplyWorkflow").click();
    await frame.waitForFunction(() => document.getElementById("studioModalRoot").classList.contains("is-hidden"));
    await frame.locator("#comfyTemporaryInfo").waitFor();
    assert.equal(await frame.locator("#comfyWorkflowChoice").inputValue(), historicalRef, "editing retains the current temporary identity");
    assert.equal(await frame.locator('[data-model-parameter="seed"]').inputValue(), "-1");
    assert.equal(await frame.locator('[data-model-parameter="seed"]').getAttribute("min"), "-1", "following the seed warning produces a usable random sentinel");
    assert.equal(await frame.locator("#comfyTemporaryInfo .comfy-seed-notice").count(), 0);
    await frame.locator("#generateButton").click(); assert.match(await frame.locator("#generationError").textContent(), /参考图/); assert.equal(submitted.length, 1);
    await openGallery(2);
    await frame.locator("#prompt").filter({ visible: true }).waitFor();
    assert.equal(await frame.locator("#prompt").inputValue(), "historical matched prompt");
    assert.equal(reproductions.length, 1); assert.equal(await frame.locator("#comfyGalleryUse").count(), 0, "associated saved workflows keep the original reproduction path");
    await openGallery(); await choose(frame, "#comfyGalleryUse", "settings"); await choose(frame, "#comfyGalleryProvider", provider.id); await frame.locator("#comfyGalleryContinue").click();
    await frame.locator("#comfyEditor").waitFor(); assert.equal(await frame.evaluate(() => window.__galleryState.view), "settings");
    assert.equal(await frame.locator("#comfyWorkflowName").isVisible(), true, "saved workflows retain the editable name");
    await assertSeedNotice(frame, "#comfySeedWarnings");
    await followSeedWarning(frame, "3");
    await frame.locator("#comfyInspect").click();
    await frame.locator("#comfyCompatibility").filter({ hasText: "模型缺失" }).waitFor();
    await followCompatibilityIssue(frame, true);
    await frame.locator("#comfyApplyWorkflow").click(); await frame.waitForFunction(() => document.getElementById("studioModalRoot").classList.contains("is-hidden"));
    assert.match(await frame.locator("#settingsDirtyStatus").textContent(), /未保存/); assert.equal(saves.length, 0);
    await frame.locator('[data-view="gallery"]').click(); await frame.locator("#staySettingsButton").click(); assert.equal(await frame.evaluate(() => window.__galleryState.view), "settings");
    await frame.locator('[data-view="gallery"]').click(); await frame.locator("#discardSettingsButton").click();
    await frame.waitForFunction(() => window.__galleryState.view === "gallery");
    assert.equal(await frame.evaluate(id => window.__settingsController.getSettings().webui.providers.find(item => item.id === id).models.length, provider.id), 0);
    await openGallery(); await choose(frame, "#comfyGalleryUse", "settings"); await choose(frame, "#comfyGalleryProvider", provider.id); await frame.locator("#comfyGalleryContinue").click();
    await frame.locator("#comfyEditor").waitFor(); await frame.locator("#comfyApplyWorkflow").click();
    await frame.waitForFunction(() => document.getElementById("studioModalRoot").classList.contains("is-hidden"));
    // Acknowledgement marks the draft saved before bootstrap/storage refreshes
    // finish. Keep that real follow-up pending to exercise the navigation guard
    // instead of assuming "已保存" also means the whole action is idle.
    let markHealthStarted;
    const healthStarted = new Promise(resolve => { markHealthStarted = resolve; });
    heldHealth = { started: markHealthStarted, gate: new Promise(resolve => { releaseHeldHealth = resolve; }) };
    await frame.locator("#saveSettingsButton").click(); await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    await Promise.race([healthStarted, new Promise((_, reject) => setTimeout(() => reject(new Error("post-save storage refresh did not start")), 10000))]);
    assert.equal(await frame.evaluate(() => window.__settingsController.isSaving()), true);
    assert.equal(await frame.locator("#saveSettingsButton").isDisabled(), true);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#appNotice").filter({ hasText: "正在保存设置，请稍候再切换页面" }).waitFor();
    assert.equal(await frame.evaluate(() => window.__galleryState.view), "settings", "navigation stays on settings until its save action finishes");
    releaseHeldHealth();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    assert.equal(await frame.evaluate(() => window.__settingsController.isSaving()), false);
    assert.equal(saves.length, 1); assert.equal((await api(page, "get", "settings/get")).webui.providers.find(item => item.id === provider.id).models.length, 1);
    available = false; await openGallery();
    await frame.locator("#appNotice").filter({ hasText: "请先在设置中添加并保存 ComfyUI 服务商" }).waitFor();
    assert.equal(await frame.locator("#comfyGalleryUse").count(), 0);
    await frame.locator("#closeDrawer").click(); available = true; rejectImport = true;
    await page.reload(); frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await openGallery();
    await frame.locator("#appNotice").filter({ hasText: "缺少可执行的 ComfyUI API 图" }).waitFor();
    assert.equal(await frame.locator("#comfyGalleryUse").count(), 0);
    assert.deepEqual(automaticSaves, []); assert.deepEqual(errors, []);
    assert.ok(inspections.length >= 3); assert.ok(imports.some(body => body.temporary_model));
  } finally { releaseHeldImport?.(); releaseHeldHealth?.(); await page.close(); }
}
(async () => {
  const browser = await playwright[process.env.STUDIO_BROWSER || "chromium"].launch({ headless: true });
  try { for (const width of [1440, 390]) await verify(browser, width); }
  finally { await browser.close(); }
  console.log(`ComfyUI gallery preparation passed; screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
