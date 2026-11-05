/* Isolated settings drafts: no production state or provider requests. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-settings-discard-"));
function deferred() { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: () => resolve() }; }

async function verify(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], posts = [], gates = [];
  let persisted, holdSave = null;
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => { if (request.method() === "POST") posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() }); });
  await page.route("**/settings/get", async route => {
    if (!persisted) { persisted = await (await route.fetch()).json(); persisted.webui.external_sources = {}; }
    await route.fulfill({ json: structuredClone(persisted) });
  });
  await page.route("**/settings/save", async route => {
    const body = route.request().postDataJSON();
    if (holdSave) { const gate = holdSave; holdSave = null; await gate.promise; }
    const revision = Number(persisted.webui.revision || 0) + 1;
    persisted = { base: body.base, webui: { ...body.studio, revision }, validation_errors: [] };
    await route.fulfill({ json: { settings_revision: revision, warnings: [] } });
  });
  await page.route("**/external/status", route => route.fulfill({ json: {
    types: [{ id: "directory", name: "自定义目录" }],
    sources: Object.entries(persisted?.webui.external_sources || {}).map(([id, value]) => ({ id, ...value, status: "complete", indexed_count: 0 })),
  } }));
  try {
    assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/);
    await page.goto(base);
    const frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
    await frame.locator('[data-view="settings"]').click();
    const discard = frame.locator("#discardSettingsChangesButton");
    const saved = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    const dirty = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "有未保存" }).waitFor();
    const history = frame.locator("#historyRecords");
    const providerName = frame.locator('[data-provider-field="name"]');
    await saved(); await discard.waitFor({ state: "hidden" });
    const initialHistory = await history.inputValue(), initialProvider = await providerName.inputValue();
    const initialTheme = await frame.evaluate(() => window.ImageStudioAppearance.get());
    const initialSort = await frame.locator("#gallerySort").inputValue();
    const nextSort = initialSort === "created" ? "latest_content" : "created";
    await history.fill(String(Number(initialHistory) + 1));
    await providerName.fill("未保存服务商");
    await frame.locator('[data-appearance-field="glassOpacity"]').evaluate(input => { input.value = "43"; input.dispatchEvent(new Event("input", { bubbles: true })); });
    await frame.locator("#gallerySort").selectOption(nextSort, { force: true });
    await frame.locator("#addExternalSource").click();
    await frame.locator("#externalEditorType").selectOption("directory", { force: true });
    await frame.locator("#externalEditorName").fill("待放弃外部图库");
    await frame.locator("#externalEditorPath").fill("/isolated/never-scanned-images");
    await frame.locator("#externalEditorApply").click();
    await dirty(); await discard.waitFor();
    assert.equal(await discard.textContent(), "放弃更改");
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await page.screenshot({ path: path.join(output, `${engine}-${width}-footer.png`), animations: "disabled" });
    await discard.click();
    await frame.locator("#confirmDiscardSettingsButton").waitFor();
    assert.equal(await history.inputValue(), String(Number(initialHistory) + 1), "opening confirmation must not discard the draft");
    await frame.locator("#cancelDiscardSettingsButton").click();
    await dirty();
    assert.equal(await providerName.inputValue(), "未保存服务商");
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.get().glassOpacity), .43);
    assert.equal(await frame.locator(".external-source-entry").count(), 1);
    assert.equal(posts.length, 0, "opening or cancelling discard must never write settings");
    await discard.click();
    await page.screenshot({ path: path.join(output, `${engine}-${width}-confirm.png`), animations: "disabled" });
    await frame.locator("#confirmDiscardSettingsButton").click();
    await saved(); await discard.waitFor({ state: "hidden" });
    await frame.locator("#settingsView.is-active").waitFor();
    assert.equal(await history.inputValue(), initialHistory);
    assert.equal(await providerName.inputValue(), initialProvider);
    assert.equal(await frame.locator(".external-source-entry").count(), 0);
    assert.deepEqual(await frame.evaluate(() => window.ImageStudioAppearance.get()), initialTheme);
    assert.equal(await frame.locator("#gallerySort").inputValue(), initialSort);
    assert.equal(posts.length, 0, "discard is local and must not save or scan external sources");

    // A successful save advances the discard baseline, and saving disables it.
    const committed = String(Number(initialHistory) + 10);
    await history.fill(committed); await dirty();
    const gate = deferred(); gates.push(gate); holdSave = gate;
    const started = page.waitForRequest(request => request.url().endsWith("/settings/save") && request.method() === "POST");
    await frame.locator("#saveSettingsButton").click(); await started;
    assert.equal(await discard.isDisabled(), true, "discard must not race an in-flight save");
    gate.resolve();
    await frame.locator("#saveSettingsButton:not(:disabled)").waitFor();
    await saved(); await discard.waitFor({ state: "hidden" });
    await history.fill(String(Number(initialHistory) + 20)); await dirty();
    await discard.click();
    await frame.locator("#confirmDiscardSettingsButton").click();
    await saved();
    assert.equal(await history.inputValue(), committed, "discard restores the most recently saved settings, not the initial page state");
    assert.equal(posts.filter(item => item.path.endsWith("/settings/save")).length, 1);
    assert.equal(posts.filter(item => !item.path.endsWith("/settings/save")).length, 0);
    const dimensions = await frame.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(dimensions.scroll <= dimensions.width + 1, JSON.stringify(dimensions));
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: dirty-only footer, cancel/confirm, complete draft restoration, saved baseline and save guard passed`);
  } finally { gates.forEach(gate => gate.resolve()); await page.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try { for (const width of [1440, 390, 320]) await verify(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
