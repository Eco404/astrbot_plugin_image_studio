/* Run only against tests.support.webui_harness; no deployment settings or real providers. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-negative-tool-"));

async function matrix(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 }, hasTouch: width < 540 });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const providerId = `negative-tool-${engine}-${width}-${Date.now()}`;
  const modelId = "negative-model";
  let frame;
  async function settings() { return (await page.request.get(`${apiRoot}/settings/get`)).json(); }
  async function saveSettings(value) {
    const response = await page.request.post(`${apiRoot}/settings/save`, { data: { base: value.base, studio: value.webui, settings_revision: value.webui.revision } });
    assert.equal(response.status(), 200, await response.text());
  }
  async function reload() {
    await page.waitForLoadState("networkidle");
    await page.goto(base);
    await page.locator("#studio").waitFor();
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${providerId}"]`).click();
    await frame.locator(`[data-settings-model="${modelId}"]`).click();
    await frame.locator('[data-model-tab="tool"]').click();
  }
  async function openNegative() { await frame.locator('[data-edit-tool-parameter="negative_prompt"]').click(); }
  async function toggle(selector, checked) {
    const input = frame.locator(selector);
    if (await input.isChecked() !== checked) await input.locator("..").click();
    assert.equal(await input.isChecked(), checked);
  }
  async function savePage() {
    const saved = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/settings/save"));
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await saved).status(), 200);
    await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
  }
  async function currentModel() { return (await settings()).webui.providers.find(item => item.id === providerId).models[0]; }
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/, "isolated harness required");
    const initial = await settings();
    initial.webui.providers.push({ id: providerId, name: "Negative parameter fixture", kind: "custom_json", enabled: true, base_url: "https://example.test", models: [{ id: modelId, name: "Negative parameter", supports_text2img: true, supports_img2img: false, supports_negative_prompt: true, negative_prompt_default: "model default negative", parameters: { steps: { type: "integer", default: 20 } }, tool: { negative_prompt_exposed: false } }] });
    await saveSettings(initial);
    await reload();
    assert.equal(await frame.locator('[data-edit-tool-parameter="negative_prompt"]').count(), 1);
    assert.equal(await frame.locator('[data-model-field="tool_negative_prompt_exposed"]').count(), 0, "no separate exposure toggle");
    assert.equal(await frame.locator(".tool-parameter-row").filter({ has: frame.locator('[data-edit-tool-parameter="negative_prompt"]') }).locator(":scope > span:not(.schema-parameter-label)").innerText(), "未暴露");
    await openNegative();
    assert.equal(await frame.locator("#parameterDialogTitle").textContent(), "编辑工具参数：反向提示词");
    assert.equal(await frame.locator("#toolParameterExposed").isChecked(), false, "legacy opt-out survives normalization");
    assert.match(await frame.locator("#toolParameterDescription").inputValue(), /反向提示词/);
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "");
    assert.match(await frame.locator("#toolParameterDefaultHint").innerText(), /模型配置中的默认值/);
    await toggle("#toolParameterExposed", true);
    await frame.locator("#toolParameterDescription").fill("Discard this edit");
    await frame.locator("#toolParameterDefault").fill("discarded override");
    await frame.locator("#parameterDialogCancel").click();
    await openNegative();
    assert.equal(await frame.locator("#toolParameterExposed").isChecked(), false);
    assert.notEqual(await frame.locator("#toolParameterDescription").inputValue(), "Discard this edit", "cancel keeps prior description");
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "");
    await toggle("#toolParameterExposed", true);
    await frame.locator("#toolParameterDescription").fill("Only list unwanted visual details.");
    await frame.locator("#toolParameterDefault").fill("tool default negative");
    await page.screenshot({ path: path.join(output, `${engine}-${width}-negative-dialog.png`) });
    await frame.locator("#parameterDialogApply").click();
    await frame.locator('[data-model-tab="model"]').click();
    assert.equal(Object.hasOwn(JSON.parse(await frame.locator("#modelParametersSchema").inputValue()), "negative_prompt"), false, "tool-only parameter does not enter model schema");
    assert.equal(await frame.locator('[data-model-field="negative_prompt_default"]').inputValue(), "model default negative");
    await frame.locator('[data-model-tab="tool"]').click();
    await openNegative();
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "tool default negative", "tab switching preserves overrides");
    await frame.locator("#parameterDialogCancel").click();
    await savePage();
    const configured = await currentModel();
    assert.deepEqual(configured.tool.parameters.negative_prompt, { exposed: true, description: "Only list unwanted visual details.", default_override: "tool default negative" });
    assert.equal(configured.tool.negative_prompt_exposed, true);
    assert.equal(configured.negative_prompt_default, "model default negative");
    await reload();
    await openNegative();
    assert.equal(await frame.locator("#toolParameterDefault").inputValue(), "tool default negative");
    assert.equal(await frame.locator("#toolParameterDescription").inputValue(), "Only list unwanted visual details.");
    assert.equal(await frame.locator("#toolParameterExposed").isChecked(), true);
    await frame.locator("#toolParameterDefault").fill("");
    await frame.locator("#parameterDialogApply").click();
    await savePage();
    assert.equal(Object.hasOwn((await currentModel()).tool.parameters.negative_prompt, "default_override"), false, "blank override falls back to model default");
    await frame.locator('[data-model-tab="model"]').click();
    await toggle('[data-model-field="supports_negative_prompt"]', false);
    await frame.locator('[data-model-tab="tool"]').click();
    assert.equal(await frame.locator('[data-edit-tool-parameter="negative_prompt"]').count(), 0, "unsupported models hide parameter");
    await frame.locator('[data-model-tab="model"]').click();
    await toggle('[data-model-field="supports_negative_prompt"]', true);
    await frame.locator('[data-model-tab="tool"]').click();
    await openNegative();
    assert.equal(await frame.locator("#toolParameterExposed").isChecked(), true, "restoring capability retains structured policy");
    await toggle("#toolParameterExposed", false);
    await frame.locator("#parameterDialogApply").click();
    await savePage();
    assert.equal((await currentModel()).tool.negative_prompt_exposed, false);
    const legacySchema = await settings();
    const legacyModel = legacySchema.webui.providers.find(item => item.id === providerId).models[0];
    legacyModel.parameters.negative_prompt = { type: "text", default: "reserved schema value" };
    legacyModel.tool.negative_prompt_exposed = false;
    legacyModel.tool.parameters.negative_prompt.exposed = true;
    await saveSettings(legacySchema);
    await reload();
    assert.equal(await frame.locator('[data-edit-tool-parameter="negative_prompt"]').count(), 1, "reserved legacy schema does not duplicate row");
    await openNegative();
    assert.equal(await frame.locator("#toolParameterExposed").isChecked(), true, "explicit structured exposure wins over legacy flag");
    await frame.locator("#parameterDialogCancel").click();
    await page.screenshot({ path: path.join(output, `${engine}-${width}-negative-list.png`) });
    assert.deepEqual(errors, [], "no browser runtime errors");
    console.log(JSON.stringify({ engine, width, passed: true }));
  } finally {
    const cleanup = await settings();
    cleanup.webui.providers = cleanup.webui.providers.filter(item => item.id !== providerId);
    await saveSettings(cleanup);
    await context.close();
  }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await matrix(browser, width); }
  finally { await browser.close(); }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
