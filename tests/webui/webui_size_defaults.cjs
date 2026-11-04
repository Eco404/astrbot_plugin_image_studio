/* Isolated harness: saved dimensions must survive incompatible model switches. */
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
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/);
    const initial = await (await page.request.get(`${api}/settings/get`)).json();
    initial.webui.providers.find(provider => provider.id === "nai").models[0].parameters.size.default = "竖图";
    const natural = initial.webui.providers.find(provider => provider.id === "natural");
    natural.models = [natural.models[0]];
    const alias = structuredClone(natural.models[0]);
    alias.id = "size-alias";
    alias.parameters.dimensions = { ...alias.parameters.size, request_key: "size", default: "1024x1536", choices: [{ value: "1024x1024", label: "方图" }, { value: "1024x1536", label: "竖图" }] };
    delete alias.parameters.size;
    natural.models.push(alias);
    const saved = await page.request.post(`${api}/settings/save`, { data: { base: initial.base, studio: initial.webui, settings_revision: initial.webui.revision } });
    assert.equal(saved.status(), 200, await saved.text());

    async function load() {
      await page.goto(base);
      const frame = await (await page.locator("#studio").elementHandle()).contentFrame();
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      return frame;
    }
    let frame = await load();
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="nai"]').click();
    await change(frame, '[data-schema-default="size"]', "方图");
    const pendingSave = page.waitForResponse(response => response.url().endsWith("/settings/save") && response.request().method() === "POST");
    await frame.locator("#saveSettingsButton").click();
    assert.equal((await pendingSave).status(), 200);
    frame = await load();
    assert.equal(await frame.locator('[data-model-parameter="size"]').inputValue(), "1024x1024");

    await change(frame, "#modelChoice", "nai:nai-diffusion-4-5-full");
    assert.equal(await frame.locator('[data-model-parameter="size"]').inputValue(), "方图", "pixel dimensions from the previous model must not hide the saved NAI default");
    await frame.waitForFunction(() => document.querySelector('[data-model-parameter="size"]').closest(".field").querySelector(".studio-select-trigger").textContent.includes("方图"));
    await frame.locator("#prompt").fill("Isolated dimensions regression");
    const pendingGeneration = page.waitForResponse(response => response.url().endsWith("/studio/generate") && response.request().method() === "POST");
    await frame.locator("#generateButton").click();
    const generated = await pendingGeneration;
    assert.equal(generated.status(), 200, await generated.text());
    assert.equal(generated.request().postDataJSON().size, "方图", "the displayed default is also submitted");

    await change(frame, "#modelChoice", "natural:size-alias");
    assert.equal(await frame.locator('[data-model-parameter="dimensions"]').inputValue(), "1024x1536", "incompatible request-key aliases use their own model default");
    await change(frame, "#modelChoice", "nai:nai-diffusion-4-5-full");
    await change(frame, "#modelChoice", "natural:studio-image");
    assert.equal(await frame.locator('[data-model-parameter="size"]').inputValue(), "1024x1024", "incompatible NAI labels fall back to pixel dimensions");
    await change(frame, '[data-model-parameter="size"]', "1024x1536");
    await change(frame, "#modelChoice", "natural:size-alias");
    assert.equal(await frame.locator('[data-model-parameter="dimensions"]').inputValue(), "1024x1536", "compatible object choices survive field aliases");
    await change(frame, '[data-model-parameter="dimensions"]', "1024x1024");
    await change(frame, "#modelChoice", "natural:studio-image");
    await change(frame, "#modelChoice", "natural:size-alias");
    assert.equal(await frame.locator('[data-model-parameter="dimensions"]').inputValue(), "1024x1024", "compatible user choice is not reset to the new model default");
    await change(frame, "#modelChoice", "nai:nai-diffusion-4-5-full");
    await change(frame, "#modelChoice", "natural:size-alias");
    assert.equal(await frame.locator('[data-model-parameter="dimensions"]').inputValue(), "1024x1024", "a remembered compatible field value remains available after visiting another provider");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: persisted dimensions, cross-provider defaults, compatible selections and request payload passed`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await run(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
