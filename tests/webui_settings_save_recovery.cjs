/* Only run against an isolated harness; all plugin-setting writes are mocked. */
const assert = require("node:assert/strict");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

function deferred() { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: () => resolve() }; }

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  let persisted, nextGetFailures = 0, getFailures = 0, bootstrapFailures = 0, gate;
  const posts = [], errors = [], releases = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/settings/get", async route => {
    if (getFailures > 0) { getFailures--; await route.fulfill({ status: 500, json: { message: "模拟设置重新读取失败" } }); return; }
    if (!persisted) {
      persisted = await (await route.fetch()).json();
      persisted.webui.external_sources = {};
    }
    await route.fulfill({ json: structuredClone(persisted) });
  });
  await page.route("**/settings/save", async route => {
    const body = route.request().postDataJSON();
    posts.push(structuredClone(body));
    if (Number(body.settings_revision) !== Number(persisted.webui.revision)) {
      await route.fulfill({ status: 409, json: { message: "过期设置版本" } }); return;
    }
    if (gate) { const waiting = gate; gate = null; await waiting.promise; }
    const revision = Number(persisted.webui.revision) + 1;
    persisted = { base: structuredClone(body.base), webui: structuredClone(body.studio), validation_errors: [] };
    persisted.webui.revision = revision;
    persisted.webui.ui = { ...persisted.webui.ui, settings_revision: revision };
    getFailures = nextGetFailures; nextGetFailures = 0;
    await route.fulfill({ json: { settings_revision: revision, warnings: [] } });
  });
  await page.route("**/studio/bootstrap", async route => {
    if (bootstrapFailures > 0) { bootstrapFailures--; await route.fulfill({ status: 500, json: { message: "模拟生图面板刷新失败" } }); return; }
    await route.continue();
  });
  await page.route("**/external/status", route => route.fulfill({ json: { types: [{ id: "nai", name: "NAI 插件图库" }], sources: [] } }));
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
    await frame.locator('[data-view="settings"]').click();
    const dirty = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "有未保存" }).waitFor();
    const clean = () => frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    const saveFinished = () => frame.locator("#saveSettingsButton").filter({ hasText: "保存全部设置" }).waitFor();
    const history = frame.locator("#historyRecords");
    await clean();
    const initial = Number(await history.inputValue());
    const revision = Number(persisted.webui.revision);
    const discard = async () => {
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#discardSettingsButton").click();
      await frame.locator("#galleryView.is-active").waitFor();
      await frame.locator('[data-view="settings"]').click();
      await clean();
    };
    const save = async newer => {
      const request = page.waitForRequest(request => request.url().endsWith("/settings/save") && request.method() === "POST");
      const wait = deferred(); releases.push(wait); gate = wait;
      await frame.locator("#saveSettingsButton").click(); await request;
      if (newer !== undefined) await history.fill(String(newer));
      wait.resolve(); await saveFinished();
    };

    // POST is authoritative even when GET fails. Keep the newer draft, while
    // committing theme/sort and retaining the acknowledged revision for retry.
    await history.fill(String(initial + 10));
    await frame.evaluate(() => window.ImageStudioAppearance.set({ glassOpacity: .46 }));
    await frame.locator("#gallerySort").selectOption("latest_content", { force: true });
    nextGetFailures = 2;
    await save(initial + 20); await dirty();
    await frame.locator("#settingsError").filter({ hasText: "已保存，但重新读取失败" }).waitFor();
    assert.equal(await history.inputValue(), String(initial + 20));
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.isDirty()), false);
    assert.equal(await frame.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "latest_content");
    await discard();
    assert.equal(await history.inputValue(), String(initial + 10), "discard restores the committed POST snapshot when retry GET also fails");
    await history.fill(String(initial + 30));
    await save(); await clean();
    assert.equal(posts[1].settings_revision, revision + 1, "next save must use the acknowledged revision");
    assert.equal(await history.inputValue(), String(initial + 30), "retry read preserves dirty settings before the next POST");

    // A later bootstrap failure must not roll back the canonical snapshot or
    // prevent browser-only settings from being committed.
    await history.fill(String(initial + 40));
    await frame.evaluate(() => window.ImageStudioAppearance.set({ glassOpacity: .57 }));
    await frame.locator("#gallerySort").selectOption("created", { force: true });
    bootstrapFailures = 1;
    await save(initial + 50); await dirty();
    await frame.locator("#settingsError").filter({ hasText: "已保存，但生图面板刷新失败" }).waitFor();
    assert.equal(await frame.evaluate(() => window.ImageStudioAppearance.isDirty()), false);
    assert.equal(await frame.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "created");
    assert.equal(await history.inputValue(), String(initial + 50));
    await discard();
    assert.equal(await history.inputValue(), String(initial + 40));
    await history.fill(String(initial + 60));
    await save(); await clean();
    assert.equal(posts[3].settings_revision, revision + 3);
    assert.deepEqual(errors, []);
    console.log(`PASS ${name} ${width}: POST acknowledgement, failed reread/bootstrap, discard, revision retry and concurrent drafts`);
  } finally {
    releases.forEach(item => item.resolve());
    await page.close();
  }
}

(async () => {
  for (const [name, launcher, width] of [["chromium", chromium, 1440], ["webkit", webkit, 390]]) {
    const browser = await launcher.launch({ headless: true });
    try { await verify(browser, name, width); } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
