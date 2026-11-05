/* Real settings validation against the isolated harness; no provider requests. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;

async function api(page, endpoint, body) {
  const response = body === undefined ? await page.request.get(`${apiRoot}/${endpoint}`) : await page.request.post(`${apiRoot}/${endpoint}`, { data: body });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const value = await response.json();
  return value.data || value;
}

async function seed(page) {
  const initial = await api(page, "settings/get");
  const template = initial.webui.providers.find(provider => provider.kind === "openai_images");
  const providers = ["a", "b"].map(letter => ({
    ...structuredClone(template), id: `identity-${letter}`, name: `Provider ${letter.toUpperCase()}`,
    api_key: "", base_url: `https://${letter}.example.test/v1`, discovered_models: [],
    models: [1, 2, 3].map(index => ({
      ...structuredClone(template.models[0]), id: `${letter}-${index}`, name: `Model ${letter.toUpperCase()}${index}`,
      native_batch_size: 1, native_batch_size_source: "manual", max_concurrent_requests: 8,
    })),
  }));
  await api(page, "settings/save", {
    settings_revision: initial.webui.revision, base: initial.base,
    studio: { ...initial.webui, providers, generation_defaults: { page: {}, tool: {} } },
  });
}

async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, options) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname.endsWith("/external/status")) return Promise.resolve(new Response(JSON.stringify({ sources: [], types: [] }), { headers: { "Content-Type": "application/json" } }));
      return original(input, options);
    };
  });
  let frame;
  const list = kind => frame.locator(kind === "provider" ? "#settingsProviderList" : "#settingsModelList");
  const rows = kind => list(kind).locator(":scope > .provider-row");
  const select = async (kind, index) => {
    await rows(kind).nth(index).locator(".settings-row-select").click();
    assert.equal(await list(kind).locator(".is-active").count(), 1, `exactly one ${kind} is selected, including duplicate/empty IDs`);
  };
  const field = (kind, key) => frame.locator(`[data-${kind}-field="${key}"]`);
  const names = kind => rows(kind).locator("strong").allTextContents();
  const open = async () => {
    if (page.url() !== "about:blank") await page.waitForLoadState("networkidle");
    await page.goto(base);
    frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  };
  const save = async (errorPattern = null) => {
    const pending = page.waitForResponse(response => response.url().endsWith("/settings/save") && response.request().method() === "POST");
    await frame.locator("#saveSettingsButton").click();
    const response = await pending;
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    if (errorPattern) {
      assert.equal(response.status(), 400, "duplicate IDs must still be rejected on save");
      assert.match(await response.text(), errorPattern);
      assert.match(await frame.locator("#settingsDirtyStatus").textContent(), /有未保存/);
    } else {
      assert.equal(response.status(), 200, await response.text());
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    }
  };
  const remove = async kind => {
    await frame.locator(kind === "provider" ? "#removeProviderButton" : "#removeModelButton").click();
    await frame.locator("#confirmAccept").click();
    await frame.locator("#confirmDialog").waitFor({ state: "hidden" });
  };
  const reorderFirst = async (kind, index) => {
    await rows(kind).nth(index).locator("[data-sort-handle]").press("Home");
  };

  try {
    await seed(page);
    await open();
    await select("provider", 1);
    await field("provider", "id").fill("identity-a");
    await field("provider", "name").fill("Provider B draft");
    await field("provider", "base_url").fill("https://b-draft.example.test/v1");
    await select("provider", 0);
    assert.equal(await field("provider", "name").inputValue(), "Provider A", "editing a duplicate provider ID must not redirect later inputs into the first provider");
    assert.equal(await field("provider", "base_url").inputValue(), "https://a.example.test/v1");
    await select("provider", 1);
    assert.equal(await field("provider", "name").inputValue(), "Provider B draft");
    assert.equal(await field("provider", "base_url").inputValue(), "https://b-draft.example.test/v1");
    await save(/Provider id 重复/);
    assert.deepEqual((await api(page, "settings/get")).webui.providers.map(provider => provider.id), ["identity-a", "identity-b"], "a rejected save must leave stored settings unchanged");
    await reorderFirst("provider", 1);
    assert.deepEqual(await names("provider"), ["Provider B draft", "Provider A"], "duplicate provider IDs remain independently sortable");
    assert.equal(await field("provider", "name").inputValue(), "Provider B draft");

    // Clearing an ID is an ordinary intermediate edit, not a deselection.
    await field("provider", "id").fill("");
    await field("provider", "timeout_seconds").fill("137");
    await select("provider", 1);
    await select("provider", 0);
    assert.equal(await field("provider", "id").inputValue(), "");
    assert.equal(await field("provider", "timeout_seconds").inputValue(), "137");
    await field("provider", "id").fill("identity-b-edited");
    await save();
    assert.deepEqual((await api(page, "settings/get")).webui.providers.map(provider => [provider.id, provider.base_url]), [["identity-b-edited", "https://b-draft.example.test/v1"], ["identity-a", "https://a.example.test/v1"]], "saving repaired IDs preserves each provider's own fields");
    await open();
    assert.deepEqual(await names("provider"), ["Provider B draft", "Provider A"]);
    assert.equal(await field("provider", "id").inputValue(), "identity-b-edited");
    assert.equal(await field("provider", "timeout_seconds").inputValue(), "137");
    assert.equal(await field("provider", "base_url").inputValue(), "https://b-draft.example.test/v1");
    await select("provider", 1);
    assert.equal(await field("provider", "base_url").inputValue(), "https://a.example.test/v1");
    await select("provider", 0);

    // Deletion must remove only the selected object, never every matching ID.
    await field("provider", "id").fill("identity-a");
    await remove("provider");
    assert.deepEqual(await names("provider"), ["Provider A"]);
    assert.equal(await field("provider", "base_url").inputValue(), "https://a.example.test/v1");
    await open();

    await select("model", 1);
    await field("model", "id").fill("b-1");
    await field("model", "name").fill("Model B2 draft");
    await field("model", "max_concurrent_requests").fill("3");
    await select("model", 0);
    assert.equal(await field("model", "name").inputValue(), "Model B1", "editing a duplicate model ID must not redirect later inputs into the first model");
    assert.equal(await field("model", "max_concurrent_requests").inputValue(), "8");
    await select("model", 1);
    assert.equal(await field("model", "name").inputValue(), "Model B2 draft");
    assert.equal(await field("model", "max_concurrent_requests").inputValue(), "3");
    await save(/模型 id 重复/);
    assert.deepEqual((await api(page, "settings/get")).webui.providers[0].models.map(model => model.id), ["b-1", "b-2", "b-3"]);
    await reorderFirst("model", 1);
    assert.deepEqual(await names("model"), ["Model B2 draft", "Model B1", "Model B3"]);
    assert.equal(await field("model", "name").inputValue(), "Model B2 draft");
    await field("model", "id").fill("");
    await field("model", "native_batch_size").fill("2");
    await select("model", 1);
    await select("model", 0);
    assert.equal(await field("model", "id").inputValue(), "");
    assert.equal(await field("model", "native_batch_size").inputValue(), "2");
    await field("model", "id").fill("b-2-edited");
    await save();
    await open();
    assert.deepEqual(await names("model"), ["Model B2 draft", "Model B1", "Model B3"]);
    assert.equal(await field("model", "id").inputValue(), "b-2-edited");
    assert.equal(await field("model", "max_concurrent_requests").inputValue(), "3");
    assert.equal(await field("model", "native_batch_size").inputValue(), "2");
    const saved = await api(page, "settings/get");
    assert.deepEqual(saved.webui.providers[1].models.map(model => [model.id, model.name]), [["a-1", "Model A1"], ["a-2", "Model A2"], ["a-3", "Model A3"]], "edits remain scoped to the selected provider");
    await field("model", "id").fill("b-1");
    await remove("model");
    assert.deepEqual(await names("model"), ["Model B1", "Model B3"]);
    assert.equal(await field("model", "max_concurrent_requests").inputValue(), "8");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: duplicate/empty IDs, independent editing/selection/sorting/deletion, rejected saves and recovery persistence passed`);
  } finally { await page.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390]) await verify(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
