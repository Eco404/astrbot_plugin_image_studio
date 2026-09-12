/* Run only against an isolated webui_harness.py. Plugin settings are mocked per
 * browser, while appearance/display preferences use the real browser cookies. */
const assert = require("node:assert/strict");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

function deferred() {
  let resolve;
  return { promise: new Promise(done => { resolve = done; }), resolve: () => resolve() };
}

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], posts = [], gates = [];
  let persistedSettings, holdSettings, holdAppearance, failAppearance = false, frame;
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (request.method() === "POST") posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
  });
  const count = suffix => posts.filter(item => item.path.endsWith(suffix)).length;
  await page.route("**/settings/get", async route => {
    if (!persistedSettings) {
      const response = await route.fetch();
      persistedSettings = await response.json();
      persistedSettings.webui.external_sources = {};
    }
    await route.fulfill({ json: structuredClone(persistedSettings) });
  });
  await page.route("**/settings/save", async route => {
    const body = route.request().postDataJSON();
    if (holdSettings) { const gate = holdSettings; holdSettings = null; await gate.promise; }
    persistedSettings = { ...persistedSettings, base: body.base, webui: body.studio };
    persistedSettings.webui.revision = (persistedSettings.webui.revision || 0) + 1;
    await route.fulfill({ json: { warnings: [] } });
  });
  await page.route("**/external/status", route => route.fulfill({ json: {
    types: [{ id: "nai", name: "NAI 插件图库", path: "/data/nai/image_history" }, { id: "directory", name: "自定义目录" }],
    sources: Object.entries(persistedSettings?.webui.external_sources || {}).map(([id, value]) => ({ id, ...value, status: "complete", indexed_count: 0 })),
  } }));
  await page.route("**/appearance", async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    if (failAppearance) { await route.fulfill({ status: 500, json: { message: "模拟主题保存失败" } }); return; }
    if (holdAppearance) { const gate = holdAppearance; holdAppearance = null; await gate.promise; }
    await route.continue();
  });
  const open = async () => {
    await page.goto(base);
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  };
  const dirty = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "有未保存" }).waitFor();
  const saved = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  const active = view => frame.locator(`#${view}View.is-active`).waitFor();
  const previewOpacity = async value => {
    await frame.locator('[data-appearance-field="glassOpacity"]').evaluate((input, value) => {
      input.value = String(value); input.dispatchEvent(new Event("input", { bubbles: true }));
    }, value);
    await dirty();
  };
  const leave = async (target, discard) => {
    await frame.locator(`[data-view="${target}"]`).click();
    await frame.locator("#studioModalTitle").filter({ hasText: "有未保存的设置" }).waitFor();
    assert.equal(await frame.locator("#settingsView").evaluate(element => element.classList.contains("is-active")), true);
    await frame.locator(discard ? "#discardSettingsButton" : "#staySettingsButton").click();
    await frame.locator("#studioModalRoot.is-hidden").waitFor({ state: "attached" });
    await active(discard ? target : "settings");
  };
  const unloadIsBlocked = () => frame.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event); return event.defaultPrevented;
  });
  const save = async () => {
    await frame.locator("#saveSettingsButton").click();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
  };
  try {
    await open();
    const initialTheme = await frame.evaluate(() => window.ImageStudioAppearance.get());
    const initialHistory = await frame.locator("#historyRecords").inputValue();
    const providerName = frame.locator('#providerForm [data-provider-field="name"]');
    const initialProvider = await providerName.inputValue();
    assert.equal(await unloadIsBlocked(), false);

    // Editing browser-only settings previews immediately, without any writes.
    await previewOpacity(43);
    await frame.locator('[name="appearanceMode"][value="dark"]').locator("..").click();
    await frame.locator("#gallerySort").selectOption("latest_content", { force: true });
    assert.equal(await frame.evaluate(() => document.documentElement.dataset.theme), "dark");
    assert.equal(await frame.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "created");
    assert.equal(await unloadIsBlocked(), true);
    assert.equal(count("/settings/save"), 0);
    assert.equal(count("/appearance"), 0);
    assert.equal(count("/gallery/preferences"), 0);
    await leave("gallery", false);
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .43);
    assert.equal(await frame.locator("#gallerySort").inputValue(), "latest_content");
    await dirty();

    // Discard must reset normal controls and mutable provider/external drafts
    // alongside the browser-only controls, then perform the requested navigation.
    await frame.locator("#historyRecords").fill(String(Number(initialHistory) + 7));
    await providerName.fill("未保存的服务商名称");
    await frame.locator("#addExternalSource").click();
    await frame.locator("#externalEditorType").selectOption("directory", { force: true });
    await frame.locator("#externalEditorName").fill("未保存的外部图库");
    await frame.locator("#externalEditorPath").fill("/data/draft-only-images");
    await frame.locator("#externalEditorApply").click();
    await frame.locator(".external-source-entry").filter({ hasText: "未保存的外部图库" }).waitFor();
    await leave("gallery", true);
    assert.deepEqual(await frame.evaluate(() => window.ImageStudioAppearance.get()), initialTheme);
    assert.equal(await unloadIsBlocked(), false);
    await frame.locator('[data-view="settings"]').click(); await saved();
    assert.equal(await frame.locator("#historyRecords").inputValue(), initialHistory);
    assert.equal(await providerName.inputValue(), initialProvider);
    assert.equal(await frame.locator("#gallerySort").inputValue(), "created");
    assert.equal(await frame.locator(".external-source-entry").count(), 0);
    assert.equal(count("/settings/save"), 0);
    assert.equal(count("/appearance"), 0);

    // Save all persists browser preferences and survives an iframe/page reload;
    // a theme/sort-only save must not write the plugin's shared configuration.
    await previewOpacity(47);
    await frame.locator('[name="appearanceMode"][value="dark"]').locator("..").click();
    await frame.locator("#gallerySort").selectOption("latest_content", { force: true });
    await save(); await saved();
    assert.equal(count("/settings/save"), 0);
    assert.equal(count("/appearance"), 1);
    assert.equal(count("/gallery/preferences"), 1);
    assert.equal(await unloadIsBlocked(), false);
    await open();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .47);
    assert.equal(await frame.evaluate(() => document.documentElement.dataset.theme), "dark");
    assert.equal(await frame.locator("#gallerySort").inputValue(), "latest_content");

    // A failed browser save preserves the preview, dirty state, and leave guard.
    failAppearance = true;
    await previewOpacity(54); await save(); await dirty();
    await frame.locator("#settingsError").filter({ hasText: /保存失败|模拟主题|HTTP|500/ }).waitFor();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .54);
    assert.equal(await unloadIsBlocked(), true);
    await leave("generate", false); await dirty();
    failAppearance = false;
    await leave("generate", true);
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .47);

    // A slow save may complete after another edit: commit the submitted snapshot
    // while keeping the newer preview and retaining the navigation warning.
    await frame.locator('[data-view="settings"]').click(); await saved();
    await previewOpacity(59);
    const appearanceGate = deferred(); gates.push(appearanceGate); holdAppearance = appearanceGate;
    const appearanceStarted = page.waitForRequest(request => request.url().endsWith("/appearance") && request.method() === "POST");
    await frame.locator("#saveSettingsButton").click(); await appearanceStarted;
    await previewOpacity(63);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "正在保存设置" }).waitFor();
    assert.equal(await frame.locator("#settingsView").evaluate(element => element.classList.contains("is-active")), true);
    appearanceGate.resolve();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor(); await dirty();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .63);
    await leave("generate", true);
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .59);

    // Repeat the in-flight edit case for ordinary plugin settings; a successful
    // response must not replace fields the user changed after pressing save.
    await frame.locator('[data-view="settings"]').click(); await saved();
    const submittedHistory = String(Number(initialHistory) + 10), newerHistory = String(Number(initialHistory) + 20);
    await frame.locator("#historyRecords").fill(submittedHistory); await dirty();
    const settingsGate = deferred(); gates.push(settingsGate); holdSettings = settingsGate;
    const settingsStarted = page.waitForRequest(request => request.url().endsWith("/settings/save") && request.method() === "POST");
    await frame.locator("#saveSettingsButton").click(); await settingsStarted;
    await frame.locator("#historyRecords").fill(newerHistory);
    settingsGate.resolve();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor(); await dirty();
    assert.equal(await frame.locator("#historyRecords").inputValue(), newerHistory);
    await leave("gallery", true);
    await frame.locator('[data-view="settings"]').click(); await saved();
    assert.equal(await frame.locator("#historyRecords").inputValue(), submittedHistory);
    assert.equal(count("/settings/save"), 1);
    await open();
    assert.equal(await frame.locator("#historyRecords").inputValue(), submittedHistory);
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .59);
    assert.equal(await frame.locator("#gallerySort").inputValue(), "latest_content");
    assert.deepEqual(errors, []);
    console.log(`${name}: staged preferences, stay/discard, reload, failed saves and in-flight draft preservation passed`);
  } finally { gates.forEach(gate => gate.resolve()); await page.close(); }
}

(async () => {
  for (const [engine, name, width] of [[chromium, "chromium-desktop", 1440], [webkit, "webkit-mobile", 390]]) {
    const browser = await engine.launch({ headless: true });
    try { await verify(browser, name, width); } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
