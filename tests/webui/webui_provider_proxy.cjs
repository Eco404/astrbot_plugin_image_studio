/* Only run against tests/support/webui_harness.py; saves change its temporary settings. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-provider-proxy-"));

async function verify(browser, engine, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  const errors = [];
  let phase = "initial";
  page.on("pageerror", error => errors.push(`${phase}: ${error.message}`));
  page.setDefaultTimeout(15000);
  try {
    await page.goto(base);
    assert.equal(await page.locator("#studio").count(), 1);
    let frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#addProviderButton").click();
    const id = await frame.locator('[data-provider-field="id"]').inputValue();
    const proxySelector = '[data-provider-field="proxy"]';
    const enabledSelector = '[data-provider-field="enabled"]';
    assert.equal(await frame.locator(proxySelector).inputValue(), "");
    const proxy = "http://user:p%40ss@127.0.0.1:7890";
    await frame.locator(proxySelector).fill(proxy);
    await frame.locator('[data-provider-field="name"]').fill("代理配置测试");
    for (const kind of ["gemini", "nai_direct", "novelai_official", "custom_json", "openai_images"]) {
      await frame.locator('[data-provider-field="kind"]').evaluate((input, value) => {
        input.value = value;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }, kind);
      assert.equal(await frame.locator(proxySelector).inputValue(), proxy, "switching provider type preserves proxy");
      assert.equal(await frame.locator(enabledSelector).count(), 1);
      assert.equal(await frame.locator(".provider-editor-heading input").getAttribute("aria-label"), "启用生图服务商");
      assert.equal((await frame.locator(".provider-editor-heading").textContent()).trim(), "代理配置测试", "header only has the provider title as visible text");
    }
    await frame.locator(".provider-editor-heading").scrollIntoViewIfNeeded();
    const geometry = await frame.locator(".provider-editor-heading").evaluate(heading => {
      const box = heading.getBoundingClientRect();
      const title = heading.querySelector("h3").getBoundingClientRect();
      const toggle = heading.querySelector("label").getBoundingClientRect();
      return { center: Math.abs((title.top + title.bottom - toggle.top - toggle.bottom) / 2), gap: toggle.left - title.right, right: Math.abs(box.right - toggle.right), height: toggle.height, overflow: document.documentElement.scrollWidth - innerWidth };
    });
    assert.ok(geometry.center <= 1 && geometry.gap >= 12 && geometry.right <= 1, JSON.stringify(geometry));
    assert.ok(geometry.height >= 44 && geometry.overflow <= 1, JSON.stringify(geometry));
    await page.screenshot({ path: path.join(output, `${engine}-${width}.png`) });

    await frame.locator(enabledSelector).locator("..").click();
    phase = "first save";
    assert.equal(await frame.locator(enabledSelector).isChecked(), false);
    const save = async () => {
      const response = page.waitForResponse(response => response.url().endsWith("/settings/save") && response.request().method() === "POST");
      await frame.locator("#saveSettingsButton").click();
      assert.equal((await response).status(), 200);
      await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    };
    await save();
    const saved = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    assert.equal(saved.webui.providers.find(item => item.id === id).proxy, proxy);
    assert.equal(saved.webui.providers.find(item => item.id === id).enabled, false);
    // Let the save's storage/status refresh settle before navigating its iframe.
    await page.waitForLoadState("networkidle");
    phase = "reload";
    // Reopen the plugin while keeping the harness parent available for bridge calls.
    await frame.goto(frame.url());
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator(`[data-settings-provider="${id}"]`).click();
    assert.equal(await frame.locator(proxySelector).inputValue(), proxy);
    assert.equal(await frame.locator(enabledSelector).isChecked(), false);
    await frame.locator(proxySelector).fill("");
    phase = "second save";
    await frame.locator(enabledSelector).locator("..").click();
    await save();
    const cleared = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    assert.equal(cleared.webui.providers.find(item => item.id === id).proxy, "");
    assert.equal(cleared.webui.providers.find(item => item.id === id).enabled, true);
    assert.deepEqual(errors, []);
    console.log(`PASS ${engine} ${width}: proxy field, provider type changes, header toggle alignment, save/reload and clearing`);
  } finally { await page.close(); }
}

(async () => {
  for (const [engine, widths] of [["chromium", [1440, 390]], ["webkit", [390]]]) {
    if (process.env.STUDIO_BROWSER && process.env.STUDIO_BROWSER !== engine) continue;
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of widths) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
