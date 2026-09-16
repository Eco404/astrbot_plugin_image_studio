/* Real settings save/read against an isolated harness; no provider requests. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-settings-order-"));

async function api(page, endpoint, body) {
  const response = body === undefined ? await page.request.get(`${apiRoot}/${endpoint}`) : await page.request.post(`${apiRoot}/${endpoint}`, { data: body });
  assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
  const value = await response.json(); return value.data || value;
}

async function seed(page) {
  const original = await api(page, "settings/get"), example = original.webui.providers.find(provider => provider.kind === "openai_images") || original.webui.providers[0];
  const template = example.models[0];
  const providers = ["a", "b", "c"].map(letter => ({ ...structuredClone(example), id: `sort-${letter}`, name: `排序服务商 ${letter.toUpperCase()}`, kind: "openai_images", enabled: true, base_url: "https://example.test/v1", api_key: "", models: [1, 2, 3].map(index => ({ ...structuredClone(template), id: `${letter}-${index}`, name: `模型 ${letter.toUpperCase()}${index}`, supports_text2img: true, supports_img2img: true, max_reference_images: 4, tool: { enabled: true, max_reference_images: 4, parameters: {} } })) }));
  const graph = { "1": { class_type: "EmptyImage", inputs: { width: 64, height: 64, batch_size: 1, color: 0 } }, "2": { class_type: "SaveImage", inputs: { images: ["1", 0], filename_prefix: "sort-fixture" } } };
  providers[2].kind = "comfyui"; providers[2].base_url = "http://example.test:8188";
  providers[2].models = [1, 2, 3].map(index => ({ id: `c-${index}`, name: `工作流 C${index}`, comfyui: { api_graph: graph, bindings: {}, outputs: ["2"] }, parameters: {}, supports_text2img: true, supports_img2img: false, max_reference_images: 1, tool: { enabled: true, parameters: {} } }));
  const defaults = { page: { text2img_model_ref: "sort-a:a-2", img2img_model_ref: "sort-a:a-1" }, tool: { text2img_model_ref: "sort-b:b-3", img2img_model_ref: "sort-b:b-2" } };
  await api(page, "settings/save", { settings_revision: original.webui.revision, base: original.base, studio: { ...original.webui, providers, generation_defaults: defaults } });
  const saved = await api(page, "settings/get");
  assert.deepEqual(saved.webui.generation_defaults, defaults);
  return { providers, defaults };
}

async function verify(browser, engine, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600, colorScheme: width < 600 ? "dark" : "light" });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  // Resolve unrelated periodic polling in the host bridge itself. A routed
  // mock still enters WebKit's network/CORS layer during iframe navigation.
  // Configuration reads and writes continue to use the real isolated API.
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window);
    window.fetch = (input, options) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname.endsWith("/external/status")) return Promise.resolve(new Response(JSON.stringify({ sources: [], types: [] }), { headers: { "Content-Type": "application/json" } }));
      return fetch(input, options);
    };
  });
  let frame;
  const providerOrder = () => frame.locator("#settingsProviderList > [data-settings-order-id]").evaluateAll(items => items.map(item => item.dataset.settingsOrderId));
  const modelOrder = () => frame.locator("#settingsModelList > [data-settings-order-id]").evaluateAll(items => items.map(item => item.dataset.settingsOrderId));
  const row = (kind, id) => frame.locator(`${kind === "provider" ? "#settingsProviderList" : "#settingsModelList"} > [data-settings-order-id="${id}"]`);
  const handle = (kind, id) => row(kind, id).locator("[data-sort-handle]");
  const selected = kind => frame.locator(`${kind === "provider" ? "#settingsProviderList" : "#settingsModelList"} > .is-active`).getAttribute("data-settings-order-id");
  const open = async () => {
    // Let settings follow-up reads finish before navigating the iframe; a
    // WebKit-aborted host bridge request otherwise looks like a CORS failure.
    if (page.url() !== "about:blank") await page.waitForLoadState("networkidle");
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1);
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  };
  const save = async () => {
    await frame.locator("#saveSettingsButton").click();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  };
  const drag = async (kind, fromId, toId, touch = false) => {
    const grid = frame.locator(kind === "provider" ? "#settingsProviderList" : "#settingsModelList");
    await grid.scrollIntoViewIfNeeded();
    const from = await handle(kind, fromId).boundingBox(), to = await handle(kind, toId).boundingBox();
    const x = from.x + from.width / 2, y = from.y + from.height / 2;
    const endX = to.x + to.width / 2 - 15, endY = to.y + to.height / 2;
    if (touch && engine === "chromium") {
      const client = await page.context().newCDPSession(page);
      try {
        await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
        await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: endX, y: endY }] });
        await frame.locator(".studio-sort-ghost").waitFor();
        assert.doesNotMatch(await frame.locator(".studio-sort-ghost").textContent(), /移动图片/);
        await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      } finally { await client.detach(); }
    } else {
      await page.mouse.move(x, y); await page.mouse.down();
      await page.mouse.move(endX, endY, { steps: 8 });
      await frame.locator(".studio-sort-ghost").waitFor();
      assert.doesNotMatch(await frame.locator(".studio-sort-ghost").textContent(), /移动图片/);
      await page.mouse.up();
    }
    await frame.locator(".studio-sort-ghost").waitFor({ state: "hidden" });
  };

  try {
    const { defaults } = await seed(page);
    await open();
    await frame.locator('[data-settings-provider="sort-a"]').click();
    await frame.locator('[data-settings-model="a-2"]').click();
    await frame.locator('[data-model-tab="tool"]').click();
    const description = frame.locator('[data-model-field="tool_selection_description"]');
    await description.fill("排序时保留工具输入草稿");
    await description.evaluate(element => { window.__orderEditor = element; });
    await frame.locator('[data-provider-field="name"]').fill("排序服务商 A 的草稿");
    await drag("provider", "sort-c", "sort-a", width < 600);
    assert.deepEqual(await providerOrder(), ["sort-c", "sort-a", "sort-b"]);
    assert.equal(await selected("provider"), "sort-a", "dragging an inactive row does not select it");
    assert.equal(await selected("model"), "a-2");
    assert.equal(await description.inputValue(), "排序时保留工具输入草稿");
    assert.equal(await description.evaluate(element => element === window.__orderEditor), true, "sorting keeps the existing editor DOM mounted");
    assert.equal(await frame.locator('[data-model-tab="tool"]').evaluate(element => element.classList.contains("is-active")), true);
    await drag("model", "a-3", "a-1", width < 600);
    assert.deepEqual(await modelOrder(), ["a-3", "a-1", "a-2"]);
    assert.equal(await selected("model"), "a-2");
    assert.equal(await description.inputValue(), "排序时保留工具输入草稿");
    const unsaved = await api(page, "settings/get");
    assert.deepEqual(unsaved.webui.providers.map(item => item.id), ["sort-a", "sort-b", "sort-c"], "sorting does not write before Save All");
    assert.deepEqual(unsaved.webui.providers[0].models.map(item => item.id), ["a-1", "a-2", "a-3"]);
    assert.deepEqual(unsaved.webui.generation_defaults, defaults);
    assert.deepEqual(await frame.locator('#settingPageDefaultTextModel,#settingPageDefaultImageModel,#settingToolDefaultTextModel,#settingToolDefaultImageModel').evaluateAll(inputs => inputs.map(input => input.value)), [defaults.page.text2img_model_ref, defaults.page.img2img_model_ref, defaults.tool.text2img_model_ref, defaults.tool.img2img_model_ref]);

    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#staySettingsButton").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.deepEqual(await providerOrder(), ["sort-c", "sort-a", "sort-b"]);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#discardSettingsButton").click();
    await frame.locator("#galleryView.is-active").waitFor();
    await frame.locator('[data-view="settings"]').click();
    assert.deepEqual(await providerOrder(), ["sort-a", "sort-b", "sort-c"]);
    await frame.locator('[data-settings-provider="sort-a"]').click();
    assert.deepEqual(await modelOrder(), ["a-1", "a-2", "a-3"]);
    assert.equal(await frame.locator('[data-provider-field="name"]').inputValue(), "排序服务商 A");

    await handle("provider", "sort-c").press("Home");
    assert.deepEqual(await providerOrder(), ["sort-c", "sort-a", "sort-b"]);
    await frame.locator('[data-settings-provider="sort-c"]').click();
    await handle("model", "c-3").press("Home");
    assert.deepEqual(await modelOrder(), ["c-3", "c-1", "c-2"]);
    await save();
    const committed = await api(page, "settings/get");
    assert.deepEqual(committed.webui.providers.map(item => item.id), ["sort-c", "sort-a", "sort-b"]);
    assert.deepEqual(committed.webui.providers[0].models.map(item => item.id), ["c-3", "c-1", "c-2"]);
    assert.deepEqual(committed.webui.providers.find(item => item.id === "sort-a").models.map(item => item.id), ["a-1", "a-2", "a-3"], "workflow sorting is scoped to its provider");
    assert.deepEqual(committed.webui.generation_defaults, defaults, "page/tool defaults do not change when order is saved");
    await open();
    assert.deepEqual(await providerOrder(), ["sort-c", "sort-a", "sort-b"]);
    assert.deepEqual(await modelOrder(), ["c-3", "c-1", "c-2"]);

    await frame.locator("#addProviderButton").click();
    const addedProvider = await frame.locator('[data-provider-field="id"]').inputValue();
    await handle("provider", addedProvider).press("Home");
    assert.equal((await providerOrder())[0], addedProvider, "new rows participate in sorting");
    await frame.locator("#removeProviderButton").click(); await frame.locator("#confirmAccept").click();
    await frame.locator("#confirmDialog").waitFor({ state: "hidden" });
    await handle("provider", "sort-b").press("Home");
    assert.equal((await providerOrder())[0], "sort-b", "sort handlers survive removal and rerendering");
    await frame.locator('[data-settings-provider="sort-a"]').click();
    await frame.locator('[data-model-tab="model"]').click();
    await frame.locator("#newModelChoice").fill("a-added"); await frame.locator("#addModelButton").click();
    await handle("model", "a-added").press("Home");
    assert.equal((await modelOrder())[0], "a-added");
    await frame.locator("#removeModelButton").click(); await frame.locator("#confirmAccept").click();
    await frame.locator("#confirmDialog").waitFor({ state: "hidden" });
    await handle("model", "a-3").press("Home");
    assert.deepEqual(await modelOrder(), ["a-3", "a-1", "a-2"]);
    await save();
    assert.deepEqual((await api(page, "settings/get")).webui.generation_defaults, defaults);
    await frame.locator("#settingsProviderList").scrollIntoViewIfNeeded();
    const geometry = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
    assert.ok(geometry.content <= geometry.width + 1, JSON.stringify(geometry));
    await frame.waitForFunction(() => Array.from(document.querySelectorAll("#settingsProviderList .provider-row")).every(element => element.getAnimations().every(animation => !["running", "pending"].includes(animation.playState))));
    await page.screenshot({ path: path.join(output, `${engine}-${width}-settings-order.png`) });
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: provider/workflow drag order, preserved drafts, save/discard, explicit defaults and dynamic rows passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of ["chromium", "webkit"]) {
    if (process.env.STUDIO_BROWSER && process.env.STUDIO_BROWSER !== engine) continue;
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [1440, 390]) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
