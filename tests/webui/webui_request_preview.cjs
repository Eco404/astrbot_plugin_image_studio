/* Isolated real-API preview only: no provider network calls or generation. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-request-preview-"));
function deferred() { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: () => resolve() }; }

async function api(page, endpoint, data) {
  const response = data === undefined ? await page.request.get(`${apiRoot}/${endpoint}`) : await page.request.post(`${apiRoot}/${endpoint}`, { data });
  assert.equal(response.status(), 200, `${endpoint}: ${await response.text()}`);
  return response.json();
}

async function change(frame, selector, value) {
  await frame.locator(selector).evaluate((element, next) => {
    element.value = next; element.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function seed(page) {
  const initial = await api(page, "settings/get");
  const provider = {
    id: "request-preview-fixture", name: "预览测试服务商", kind: "custom_json", enabled: true,
    base_url: "https://saved.example.test/v1", api_key: "preview-secret-must-be-redacted",
    generate_path: "/images/generations", edit_path: "/images/edits",
    request_template: JSON.stringify({ marker: "saved-template", count: "{{count}}", steps: "{{steps}}", quality: "{{quality}}", model: "{{model}}" }),
    models: [0, 1].map(index => ({
      id: `preview-model-${index}`, name: `预览模型 ${index + 1}`, supports_text2img: true, supports_img2img: true, max_reference_images: 2,
      native_batch_size: 2, native_batch_size_source: "manual", max_concurrent_requests: 2,
      parameters: {
        count: { type: "integer", default: index ? 1 : 5, min: 1, max: 8 },
        steps: { type: "integer", default: index ? 12 : 7, min: 1, max: 40, mode_overrides: { img2img: { default: index ? 21 : 18 } } },
        quality: { type: "select", default: "standard", choices: ["standard", "high"], mode_overrides: { img2img: { default: "precise", choices: ["fast", "precise"] } } },
      },
    })),
  };
  initial.webui.providers = [provider];
  initial.webui.generation_defaults = { page: {}, tool: {} };
  initial.webui.external_sources = {};
  await api(page, "settings/save", { settings_revision: initial.webui.revision, base: initial.base, studio: initial.webui });
  return api(page, "settings/get");
}

async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], posts = [], gates = [];
  let holdPreview = null;
  page.on("pageerror", error => errors.push(error.message));
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/);
    const baseline = await seed(page);
    page.on("request", request => { if (request.method() === "POST") posts.push(new URL(request.url()).pathname); });
    await page.route("**/provider/preview", async route => {
      const pending = holdPreview; holdPreview = null;
      if (!pending) { await route.continue(); return; }
      const response = await route.fetch();
      await pending.promise;
      await route.fulfill({ response });
    });
    await page.route("**/external/status", route => route.fulfill({ json: { types: [], sources: [] } }));
    await page.goto(base);
    const frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    assert.equal(await frame.locator("#discoverModelsButton").count(), 0, "custom JSON has no misleading discovery button");
    await frame.locator("#previewRequestButton").filter({ hasText: "预览请求" }).waitFor();
    await frame.locator('[data-provider-field="base_url"]').fill(`https://draft.example.test/${"long-path-".repeat(15)}`);
    await frame.locator(".provider-request-details > summary").click();
    const template = frame.locator('[data-provider-field="request_template_text2img"]');
    await template.fill(JSON.stringify({ marker: "unsaved-text-template", count: "{{count}}", steps: "{{steps}}", quality: "{{quality}}", model: "{{model}}" }));
    await frame.locator('[data-provider-field="request_template_img2img"]').fill(JSON.stringify({ marker: "unsaved-image-template", count: "{{count}}", steps: "{{steps}}", quality: "{{quality}}", image: "{{references[0]}}", model: "{{model}}" }));
    await change(frame, '[data-schema-default="steps"]', "9");
    const preview = async action => {
      const pending = page.waitForResponse(response => response.url().endsWith("/provider/preview") && response.request().method() === "POST");
      await action();
      const response = await pending;
      assert.equal(response.status(), 200, await response.text());
      const payload = await response.json();
      await frame.locator("#requestPreviewContent").filter({ hasText: payload.batches[0].url }).waitFor();
      return { payload, request: response.request().postDataJSON() };
    };
    const first = await preview(() => frame.locator("#previewRequestButton").click());
    await frame.locator("#studioModalTitle").filter({ hasText: "请求预览" }).waitFor();
    assert.equal(first.request.model_index, 0);
    assert.equal(first.request.provider.base_url, `https://draft.example.test/${"long-path-".repeat(15)}`);
    assert.equal(first.payload.mode, "text2img");
    assert.equal(first.payload.requested_count, 5);
    assert.equal(first.payload.request_count, 3);
    assert.equal(first.payload.batches.reduce((sum, batch) => sum + batch.repeat * batch.count, 0), 5);
    assert.ok(first.payload.batches.every(batch => batch.body.marker === "unsaved-text-template" && batch.body.steps === 9 && batch.body.quality === "standard"));
    assert.equal(JSON.stringify(first.payload).includes("preview-secret-must-be-redacted"), false, "preview must not expose API credentials");
    const imagePreview = await preview(() => change(frame, "#requestPreviewMode", "img2img"));
    assert.equal(imagePreview.payload.mode, "img2img");
    assert.ok(imagePreview.payload.batches.every(batch => batch.body.marker === "unsaved-image-template" && batch.body.steps === 18 && batch.body.quality === "precise"));
    assert.equal(typeof imagePreview.payload.batches[0].body.image, "string", "image mode shows the reference placeholder in the real request shape");
    const second = await preview(() => change(frame, "#requestPreviewModel", "1"));
    assert.equal(second.request.model_index, 1);
    assert.equal(second.payload.model.id, "preview-model-1");
    assert.equal(second.payload.requested_count, 1);
    assert.equal(second.payload.batches[0].body.steps, 21);

    // Rapid mode changes may complete out of order; keep the latest selection.
    const modeGate = deferred(); gates.push(modeGate); holdPreview = modeGate;
    const oldModeStarted = page.waitForRequest(request => request.url().endsWith("/provider/preview") && request.postDataJSON()?.mode === "text2img");
    await change(frame, "#requestPreviewMode", "text2img"); await oldModeStarted;
    await preview(() => change(frame, "#requestPreviewMode", "img2img"));
    await frame.locator("#requestPreviewContent").filter({ hasText: "unsaved-image-template" }).waitFor();
    const oldModeFinished = page.waitForResponse(response => response.url().endsWith("/provider/preview") && response.request().postDataJSON()?.mode === "text2img");
    modeGate.resolve(); await oldModeFinished;
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await frame.locator("#requestPreviewMode").inputValue(), "img2img");
    assert.equal((await frame.locator("#requestPreviewContent").textContent()).includes("unsaved-text-template"), false, "an older mode response must not replace the latest preview");
    const geometry = await frame.evaluate(() => {
      const root = document.documentElement, modal = document.getElementById("studioModal"), body = document.getElementById("studioModalBody");
      return { viewport: root.clientWidth, pageWidth: root.scrollWidth, modalWidth: modal.getBoundingClientRect().width, bodyWidth: body.clientWidth, bodyScroll: body.scrollWidth };
    });
    assert.ok(geometry.pageWidth <= geometry.viewport + 1, JSON.stringify(geometry));
    assert.ok(geometry.modalWidth <= geometry.viewport + 1, JSON.stringify(geometry));
    assert.ok(geometry.bodyScroll <= geometry.bodyWidth + 1, "long URL/JSON should scroll inside its content, not overflow the modal");
    await page.screenshot({ path: path.join(output, `${engine}-${width}.png`), animations: "disabled" });
    await frame.locator("#studioModalClose").click();

    // Invalid unsaved templates fail locally without saving or sending a request.
    await template.fill('{"broken":');
    const failed = page.waitForResponse(response => response.url().endsWith("/provider/preview"));
    await frame.locator("#previewRequestButton").click();
    assert.equal((await failed).status(), 400);
    await frame.locator("#requestPreviewStatus").filter({ hasText: /JSON|模板|解析|预览失败/ }).waitFor();
    await frame.locator("#studioModalClose").click();

    // A completed response from a closed preview must not replace another modal.
    await template.fill(JSON.stringify({ marker: "closed-preview-response", steps: "{{steps}}" }));
    const gate = deferred(); gates.push(gate); holdPreview = gate;
    const started = page.waitForRequest(request => request.url().endsWith("/provider/preview"));
    await frame.locator("#previewRequestButton").click(); await started;
    await frame.locator("#studioModalClose").click();
    await frame.locator("#addExternalSource").click();
    const title = await frame.locator("#studioModalTitle").textContent();
    const finished = page.waitForResponse(response => response.url().endsWith("/provider/preview"));
    gate.resolve(); await finished;
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await frame.locator("#studioModalTitle").textContent(), title);
    assert.equal(await frame.locator("#requestPreviewContent").count(), 0);
    assert.equal(await frame.locator("#studioModalBody").textContent().then(text => text.includes("closed-preview-response")), false);
    assert.deepEqual(posts.filter(value => !value.endsWith("/provider/preview")), [], "preview must never save settings, discover models, test models or generate");
    assert.deepEqual(await api(page, "settings/get"), baseline, "preview leaves persisted settings unchanged");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: unsaved requests, mode defaults, batching, model choice, validation and late-response isolation passed`);
  } finally { gates.forEach(gate => gate.resolve()); await page.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390, 320]) await verify(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
