/* No provider traffic: browser uploads, generation and quota are intercepted. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-novelai-generation-"));
const modelIds = ["nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai-diffusion-5-full", "nai-diffusion-5-curated"];
async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => { input.value = next; input.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}

async function run(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  const page = await context.newPage(), errors = [], generations = [], uploads = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    const bootstrap = await (await page.request.get(`${base}/astrbot_plugin_image_studio/studio/bootstrap`)).json();
    const provider = { id: "official", name: "NovelAI 测试", kind: "novelai_official", enabled: true };
    const models = bootstrap.novelai_models.map(model => ({ ...model, provider_id: provider.id, provider_name: provider.name, provider_kind: provider.kind, model_ref: `${provider.id}:${model.id}` }));
    models[1].parameters.characters.webui_visible = false;
    bootstrap.providers.push(provider); bootstrap.models.push(...models);
    await page.route("**/studio/bootstrap", route => route.fulfill({ json: bootstrap }));
    await page.route("**/studio/provider-quota?*", route => route.fulfill({ json: { kind: "novelai_official", remaining: 0, subscription_active: false } }));
    await page.route("**/studio/generate", route => { generations.push(route.request().postDataJSON()); return route.fulfill({ json: { images: [], provider_name: "NovelAI 测试", model: modelIds[0], elapsed_ms: 1 } }); });
    let bytes;
    await page.route("**/studio/reference/upload", route => {
      const raw = route.request().postDataBuffer(), isMask = raw.includes(Buffer.from("inpaint-mask.png"));
      uploads.push({ isMask, raw });
      return route.fulfill({ json: { id: `ref-${uploads.length}`, filename: isMask ? "inpaint-mask.png" : "reference.png", width: 64, height: 64, preview_data_url: `data:image/png;base64,${bytes.toString("base64")}` } });
    });
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    bytes = Buffer.from(await frame.evaluate(() => { const canvas = document.createElement("canvas"); canvas.width = 64; canvas.height = 64; const context = canvas.getContext("2d"); context.fillStyle = "#789"; context.fillRect(0, 0, 64, 64); return canvas.toDataURL("image/png").split(",")[1]; }), "base64");
    const upload = async count => {
      const previous = await frame.locator(".novelai-reference-card").count();
      await frame.locator("#referenceUpload").setInputFiles(Array.from({ length: count }, (_, index) => ({ name: `reference-${index}.png`, mimeType: "image/png", buffer: bytes })));
      await frame.waitForFunction(expected => document.querySelectorAll(".novelai-reference-card").length === expected && document.getElementById("referenceChooseButton").getAttribute("aria-busy") === "false", previous + count);
    };
    const generate = async () => { const count = generations.length; await frame.locator("#generateButton").click(); await frame.waitForFunction(() => !document.getElementById("generateButton").disabled); assert.equal(generations.length, count + 1, await frame.locator("#generationError").textContent()); return generations.at(-1); };
    const mode = value => choose(frame, '[data-model-parameter="reference_mode"]', value);
    const role = (index, value) => choose(frame, `[data-novelai-reference-setting="type"][data-novelai-reference-index="${index}"]`, value);
    await choose(frame, "#modelChoice", "official:" + modelIds[0]);
    assert.equal(await frame.locator('[data-model-parameter="steps"]').inputValue(), "23");
    assert.equal(await frame.locator('[data-model-parameter="straight_alpha"]').count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="variety_boost"]').count(), 1);
    await frame.locator("#prompt").fill("two travelers, forest");
    await frame.locator("[data-novelai-character-add]").click();
    await frame.locator('[data-character-field="prompt"]').fill("blue hair, explorer");
    await frame.locator('[data-character-field="negative_prompt"]').fill("hat");
    const removeBounds = await frame.locator("[data-character-remove]").boundingBox();
    assert.ok(Math.abs(removeBounds.width - removeBounds.height) <= 1, "character remove button stays circular in flex headings");
    await frame.locator('[data-model-parameter="use_coords"]').check({ force: true });
    await choose(frame, '[data-character-field="x"]', "0.3");
    let result = await generate();
    assert.deepEqual(result.parameters.characters, [{ prompt: "blue hair, explorer", negative_prompt: "hat", x: .3, y: .5 }]);
    assert.equal(result.parameters.use_coords, true);
    assert.equal(result.parameters.reference_settings, undefined);

    await frame.locator('[data-mode="img2img"]').click();
    await choose(frame, "#modelChoice", "official:" + modelIds[0]);
    await mode("precise"); await upload(2);
    assert.equal(await frame.locator('[data-novelai-reference-setting="strength"]').first().inputValue(), "0.6");
    assert.equal(await frame.locator('[data-novelai-reference-setting="fidelity"]').first().inputValue(), "0.6");
    await role(1, "style");
    await frame.locator('[data-novelai-reference-setting="strength"][data-novelai-reference-index="0"]').fill("0.6");
    await frame.locator('[data-novelai-reference-setting="fidelity"][data-novelai-reference-index="0"]').fill("0.35");
    await page.screenshot({ path: path.join(output, `${engine}-${width}-precise.png`), fullPage: true });
    await frame.locator("#referenceField").screenshot({ path: path.join(output, `${engine}-${width}-references.png`) });
    await frame.locator("[data-novelai-characters]").screenshot({ path: path.join(output, `${engine}-${width}-characters.png`) });
    result = await generate();
    assert.equal(result.parameters.reference_mode, "precise");
    assert.deepEqual(result.parameters.reference_settings.map(item => item.type), ["character", "style"]);
    assert.equal(result.parameters.reference_settings[0].strength, .6);
    assert.equal(result.parameters.reference_settings[0].fidelity, .35);
    assert.deepEqual(result.reference_ids, ["ref-1", "ref-2"]);

    await mode("vibe"); await upload(2);
    assert.equal(await frame.locator('[data-novelai-reference-setting="strength"]').first().inputValue(), "0.6");
    assert.equal(await frame.locator('[data-novelai-reference-setting="information_extracted"]').first().inputValue(), "0.7");
    await role(0, "character");
    const count = generations.length;
    await frame.locator("#generateButton").click();
    assert.equal(generations.length, count);
    assert.match(await frame.locator("#generationError").textContent(), /不能在同一次/);
    await role(0, "base");
    result = await generate();
    assert.deepEqual(result.parameters.reference_settings.map(item => item.type), ["base", "vibe"]);
    assert.equal(result.parameters.reference_settings[1].information_extracted, .7);

    await mode("img2img"); await upload(2);
    await role(1, "mask");
    assert.equal(await frame.locator('[data-model-parameter="reference_mode"]').inputValue(), "inpaint", "assigning a mask enters inpaint without dropping other image roles");
    assert.equal(await frame.locator('[data-novelai-reference-setting="type"][data-novelai-reference-index="0"]').inputValue(), "base");
    await upload(1); await role(2, "vibe");
    const beforeInpaintConflict = generations.length;
    await frame.locator("#generateButton").click();
    assert.equal(generations.length, beforeInpaintConflict);
    assert.match(await frame.locator("#generationError").textContent(), /局部重绘不支持 Vibe/);
    await role(2, "character");
    result = await generate();
    assert.deepEqual(result.parameters.reference_settings.map(item => item.type), ["base", "mask", "character"]);

    await mode("img2img"); await upload(1);
    await frame.evaluate(() => {
      const bridge = window.AstrBotPluginPage, upload = bridge.upload;
      bridge.upload = async function (endpoint, file) { if (file.name === "inpaint-mask.png") window.__testMaskBytes = Array.from(new Uint8Array(await file.arrayBuffer())); return upload.call(this, endpoint, file); };
    });
    await frame.locator("[data-novelai-paint-mask]").click();
    await frame.locator("#novelaiMaskCanvas").waitFor({ state: "visible" });
    const canvas = frame.locator("#novelaiMaskCanvas");
    await canvas.evaluate(element => {
      const bounds = element.getBoundingClientRect(), pointer = { bubbles: true, pointerId: 1, pointerType: "pen", clientX: bounds.left + bounds.width * .3, clientY: bounds.top + bounds.height * .3 };
      // A trusted pointer is required for capture; override capture only for
      // the synthetic draw and exercise the actual paint/serialization code.
      element.setPointerCapture = () => {};
      element.dispatchEvent(new PointerEvent("pointerdown", pointer));
      element.dispatchEvent(new PointerEvent("pointermove", { ...pointer, clientX: bounds.left + bounds.width * .7 }));
      element.dispatchEvent(new PointerEvent("pointerup", pointer));
    });
    await frame.locator("#studioModalFooter button").filter({ hasText: "使用蒙版" }).click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal(uploads.at(-1).isMask, true);
    // WebKit's request inspection converts multipart binary bodies to text;
    // inspect the File handed to the real bridge before that conversion.
    const png = Buffer.from(await frame.evaluate(() => window.__testMaskBytes)), signature = png.indexOf(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    assert.ok(signature >= 0); assert.equal(png.readUInt32BE(signature + 16), 64); assert.equal(png.readUInt32BE(signature + 20), 64);
    assert.equal(await frame.locator('[data-model-parameter="reference_mode"]').inputValue(), "inpaint");
    await upload(1); await role(2, "character");
    result = await generate();
    assert.deepEqual(result.parameters.reference_settings.map(item => item.type), ["base", "mask", "character"]);

    await choose(frame, "#modelChoice", "official:" + modelIds[2]);
    assert.deepEqual(await frame.locator('[data-model-parameter="reference_mode"] option').evaluateAll(options => options.map(option => option.value)), ["img2img", "inpaint"]);
    assert.equal(await frame.locator('[data-model-parameter="variety_boost"]').count(), 0);
    assert.equal(await frame.locator('[data-model-parameter="straight_alpha"]').count(), 1);
    await mode("inpaint"); await upload(2);
    assert.equal(await frame.locator("#referenceUpload").isDisabled(), true);
    assert.deepEqual(await frame.locator('[data-novelai-reference-setting="type"]').first().locator("option").evaluateAll(options => options.map(option => option.value)), ["base", "mask"]);
    await frame.locator('[data-character-field="x"]').fill("0.17");
    result = await generate();
    assert.equal(result.parameters.characters[0].x, .17);
    await choose(frame, "#modelChoice", "official:" + modelIds[3]);
    assert.match(await frame.locator("#modelParameters").textContent(), /V5 精选版的局部重绘使用 V4.5 精选版/);
    assert.match(await frame.locator("[data-novelai-character-hint]").textContent(), /最多 6 个/);
    assert.equal(await frame.locator('select[data-character-field="x"]').count(), 1);
    await frame.locator('[data-mode="text2img"]').click();
    await choose(frame, "#modelChoice", "official:" + modelIds[1]);
    assert.equal(await frame.locator("[data-novelai-characters]").count(), 0, "schema visibility controls the specialized editor");
    result = await generate();
    assert.deepEqual(result.parameters.characters, [], "hidden fields use schema defaults instead of carried characters");
    const bounds = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(bounds.scroll <= bounds.width + 1, `horizontal overflow: ${JSON.stringify(bounds)}`);
    await page.screenshot({ path: path.join(output, `${engine}-${width}.png`), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: character cards, reference roles, combinations, mask paint/upload, capability gating passed`);
  } finally { await context.close(); }
}
(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await run(browser, width); }
  finally { await browser.close(); }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
