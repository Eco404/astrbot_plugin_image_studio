/* Real WebUI rendering, isolated harness and loopback mocks only. No provider calls. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-tooltips-"));
const description = '控制参考图影响。第二行说明\n包含 <b>字面文本</b> 与 "引号"。';
const schema = {
  count: { type: "integer", label: "生图张数", request_key: "n", description: "本次请求的图片数量。", default: 1, min: 1, max: 4 },
  normalize_reference_strength_multiple: { type: "boolean", label: "归一化 Vibe 强度", request_key: "normalize_reference_strength_multiple", description, default: true },
  quality_preset: { type: "select", label: "质量标签预设", request_key: "quality", description: "选择默认质量标签。", default: "none", choices: ["none", "standard"] },
  unnamed_parameter: { type: "text", description: "未填写 label 时仍有明确标题。", default: "" },
  same_key: { type: "text", label: "same_key", default: "" },
};
async function choose(frame, selector, value) {
  await frame.locator(selector).evaluate((input, next) => { input.value = next; input.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}
const container = locator => locator.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " field ") or contains(concat(" ", normalize-space(@class), " "), " toggle-row ")][1]');

async function run(browser, engine, width) {
  const touch = width <= 720;
  const context = await browser.newContext({ viewport: { width, height: touch ? 844 : 1000 }, hasTouch: touch, isMobile: touch });
  const page = await context.newPage(), errors = [];
  page.setDefaultTimeout(10000);
  page.on("pageerror", error => errors.push(error.message));
  try {
    const settings = await (await page.request.get(`${base}/astrbot_plugin_image_studio/settings/get`)).json();
    const bootstrap = await (await page.request.get(`${base}/astrbot_plugin_image_studio/studio/bootstrap`)).json();
    const model = { ...structuredClone(settings.webui.providers[0].models[0]), id: "tooltip-fixture", name: "说明测试模型", parameters: schema, tool: { enabled: true, parameters: { count: { exposed: true, description: "工具张数说明。" } } } };
    const provider = { ...structuredClone(settings.webui.providers[0]), id: "tooltip-fixture", name: "说明测试服务商", kind: "custom_json", models: [model] };
    const official = { id: "tooltip-official", name: "NovelAI 说明测试", kind: "novelai_official", enabled: true, models: bootstrap.novelai_models };
    settings.webui.providers.push(provider, official);
    bootstrap.providers.push(provider, official);
    bootstrap.models.push(...[provider, official].flatMap(item => item.models.map(model => ({ ...model, provider_id: item.id, provider_kind: item.kind, provider_name: item.name, model_ref: `${item.id}:${model.id}` }))));
    await page.route("**/settings/get", route => route.fulfill({ json: settings }));
    await page.route("**/studio/bootstrap", route => route.fulfill({ json: bootstrap }));
    await page.route("**/studio/provider-quota?*", route => route.fulfill({ json: { kind: "novelai_official", remaining: 0, subscription_active: false } }));
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
    assert.equal(await frame.locator('.nav-item[data-tooltip], .nav-item[title]').count(), 0, "navigation does not repeat its name in a bubble");
    const popup = frame.locator("#studioTooltip");
    const tapHelp = locator => touch ? locator.tap() : locator.click();
    const hidden = () => popup.waitFor({ state: "hidden" });
    async function visible(text) {
      await popup.waitFor({ state: "visible" });
      await frame.waitForFunction(() => { const popup = document.getElementById("studioTooltip"); return popup?.classList.contains("is-visible") && popup.getAttribute("aria-hidden") === "false" && Number(getComputedStyle(popup).opacity) >= .98; });
      if (text !== undefined) assert.equal(await popup.textContent(), text);
      assert.equal(await popup.getAttribute("role"), "tooltip");
    }
    async function help(control, expectedLabel, expectedKey, expectedDescription) {
      const row = container(control);
      const button = row.locator(".parameter-help-text[data-tooltip-toggle]").first();
      assert.equal((await button.innerText()).trim(), expectedLabel, "the title itself is the help trigger");
      assert.equal(await button.count(), 1);
      assert.equal(await row.locator(".parameter-help-button, .parameter-help-text svg").count(), 0, "no separate info icon");
      const text = await button.getAttribute("data-tooltip");
      assert.equal(text.split("\n")[0], expectedKey, "hint uses request_key with field-name fallback");
      if (expectedDescription) assert.ok(text.includes(expectedDescription), text);
      if (touch) await button.tap(); else await button.hover();
      if (!touch) await page.mouse.down();
      const paint = await button.evaluate(element => {
        const style = getComputedStyle(element);
        return { background: style.backgroundColor, shadow: style.boxShadow, transform: style.transform, tap: style.webkitTapHighlightColor };
      });
      assert.equal(paint.background, "rgba(0, 0, 0, 0)");
      assert.equal(paint.shadow, "none");
      assert.equal(paint.transform, "none");
      assert.equal(paint.tap, "rgba(0, 0, 0, 0)");
      if (!touch) await page.mouse.up();
      await visible(text);
      assert.equal(await popup.locator("b").count(), 0, "descriptions are rendered as plain text");
      assert.ok((await button.getAttribute("aria-describedby") || "").split(/\s+/).includes("studioTooltip"));
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${expectedKey.slice(0, 20)}.png`) });
      await page.keyboard.press("Escape"); await hidden();
      assert.ok(!(await button.getAttribute("aria-describedby") || "").split(/\s+/).includes("studioTooltip"));
      return button;
    }
    await choose(frame, "#modelChoice", "tooltip-fixture:tooltip-fixture");
    const plainField = container(frame.locator('[data-model-parameter="same_key"]'));
    assert.equal(await plainField.locator('.schema-parameter-label').innerText(), "same_key");
    assert.equal(await plainField.locator('[data-tooltip], [data-tooltip-toggle]').count(), 0, "a parameter with no extra description does not repeat its own key");
    let invalidEvents = 0, emptyRequests = 0;
    await page.exposeFunction("recordInvalidPrompt", () => { invalidEvents++; });
    await frame.locator("#prompt").evaluate(element => element.addEventListener("invalid", () => window.recordInvalidPrompt()));
    await page.route("**/studio/generate", route => { emptyRequests++; return route.fulfill({ json: { images: [] } }); });
    for (const prompt of ["", "   "]) {
      await frame.locator("#prompt").fill(prompt);
      await frame.locator("#generateButton").click();
      assert.equal(await frame.locator("#generationError").innerText(), "请填写提示词。");
    }
    assert.equal(invalidEvents, 0, "prompt validation never opens a browser validity bubble");
    assert.equal(emptyRequests, 0, "empty prompts cannot reach a provider");
    await frame.locator("#prompt").focus();
    await help(frame.locator('[data-model-parameter="count"]'), "生图张数", "n", schema.count.description);
    const checkbox = frame.locator('[data-model-parameter="normalize_reference_strength_multiple"]');
    await help(checkbox, "归一化 Vibe 强度", "normalize_reference_strength_multiple", description);
    assert.equal(await checkbox.isChecked(), true, "opening help does not toggle a checkbox");
    await help(frame.locator('[data-model-parameter="unnamed_parameter"]'), "unnamed_parameter", "unnamed_parameter", schema.unnamed_parameter.description);
    await help(frame.locator('[data-model-parameter="quality_preset"]'), "质量标签预设", "quality", schema.quality_preset.description);
    const selected = frame.locator('.studio-select-trigger[data-select-id="modelChoice"]');
    await selected.click();
    await frame.locator('.studio-select-menu[data-select-id="modelChoice"]').waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    assert.equal(await frame.locator("#modelChoice").inputValue(), "tooltip-fixture:tooltip-fixture", "hints do not consume select interactions");
    await selected.evaluate(element => { element.dataset.tooltip = "选择生图模型"; });
    await selected.press("Tab"); await page.keyboard.press("Shift+Tab"); await visible("选择生图模型");
    await selected.press("Enter");
    await frame.locator('.studio-select-menu[data-select-id="modelChoice"]').waitFor({ state: "visible" });
    await hidden();
    await page.keyboard.press("Escape");
    assert.equal(await selected.getAttribute("aria-expanded"), "false", "the first Escape still closes an active select menu");

    // Ordinary controls retain their actions; only explicit help buttons toggle on touch.
    await frame.evaluate(() => {
      const fixture = document.createElement("section"); fixture.id = "tooltipFixture";
      fixture.style.cssText = "position:fixed;right:12px;top:140px;z-index:9000;display:flex;gap:8px;padding:12px;background:var(--glass);border-radius:18px";
      fixture.innerHTML = '<button id="tooltipAction" type="button" class="quiet-button" data-tooltip="普通操作说明" aria-describedby="existing-description">操作</button><button id="tooltipHelp" type="button" class="parameter-help-button" data-tooltip-toggle data-tooltip="帮助内容" aria-label="查看帮助">?</button><span id="existing-description" hidden>已有说明</span>';
      document.body.append(fixture);
      window.tooltipActionCount = 0;
      document.getElementById("tooltipAction").addEventListener("click", () => window.tooltipActionCount++);
    });
    const action = frame.locator("#tooltipAction"), fixtureHelp = frame.locator("#tooltipHelp");
    if (!touch) {
      await action.hover(); await visible("普通操作说明");
      assert.equal(await action.getAttribute("aria-describedby"), "existing-description studioTooltip");
      await page.keyboard.press("Escape"); await hidden();
      assert.equal(await action.getAttribute("aria-describedby"), "existing-description");
      await action.blur(); await action.focus();
      await page.waitForTimeout(700); await hidden();
      await action.press("Tab"); await page.keyboard.press("Shift+Tab"); await visible("普通操作说明");
      await page.keyboard.press("Escape"); await hidden();
    }
    await action.click();
    assert.equal(await frame.evaluate(() => window.tooltipActionCount), 1, "tooltip does not intercept ordinary button action");
    await page.keyboard.press("Escape"); await hidden();
    if (!touch) {
      await action.evaluate(element => { element.disabled = true; });
      await page.mouse.move(1, 1);
      await action.hover(); await visible("普通操作说明");
      await page.keyboard.press("Escape"); await hidden();
      await action.evaluate(element => { element.disabled = false; });
    }
    await tapHelp(fixtureHelp); await visible("帮助内容");
    await tapHelp(fixtureHelp); await hidden();
    await tapHelp(fixtureHelp); await visible("帮助内容");
    await frame.locator("#prompt").click(); await hidden();
    await tapHelp(fixtureHelp); await visible("帮助内容");
    await frame.evaluate(() => document.dispatchEvent(new Event("scroll"))); await hidden();

    // Dynamic third-party/native titles are migrated too, without duplicate hints.
    await frame.evaluate(() => { const button = document.createElement("button"); button.id = "legacyTooltip"; button.title = "动态原生说明"; button.textContent = "动态"; document.getElementById("tooltipFixture").append(button); });
    await frame.waitForFunction(() => { const button = document.getElementById("legacyTooltip"); return !button.hasAttribute("title") && button.dataset.tooltip === "动态原生说明"; });
    await frame.evaluate(() => document.getElementById("legacyTooltip").title = "更新的动态说明");
    await frame.waitForFunction(() => { const button = document.getElementById("legacyTooltip"); return !button.hasAttribute("title") && button.dataset.tooltip === "更新的动态说明"; });

    // Long hints fit narrow screens, remain readable and scroll without closing.
    for (const preference of ["light", "dark"]) {
      await frame.evaluate(preference => {
        window.ImageStudioAppearance.set({ preference, glassOpacity: .35 }, false);
        const help = document.getElementById("tooltipHelp");
        help.dataset.tooltip = "very_long_request_key_".repeat(6) + "\n" + "长说明文字不应超出屏幕。\n".repeat(90);
      }, preference);
      await tapHelp(fixtureHelp); await visible();
      const geometry = await popup.evaluate(element => {
        const box = element.getBoundingClientRect(), css = getComputedStyle(element);
        return { x: box.x, y: box.y, right: box.right, bottom: box.bottom, width: innerWidth, height: innerHeight, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, overflowY: css.overflowY, blur: css.backdropFilter || css.webkitBackdropFilter, background: css.backgroundColor };
      });
      assert.ok(geometry.x >= 0 && geometry.y >= 0 && geometry.right <= geometry.width + 1 && geometry.bottom <= geometry.height + 1, JSON.stringify(geometry));
      assert.ok(geometry.scrollHeight > geometry.clientHeight, "long descriptions have a bounded scrolling area");
      assert.ok(["auto", "scroll"].includes(geometry.overflowY));
      assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, "long field names wrap");
      assert.match(geometry.blur, /blur\(/, "tooltip uses glass material");
      const pageScroll = await frame.evaluate(() => ({ x: window.scrollX, y: window.scrollY }));
      await fixtureHelp.focus();
      await fixtureHelp.press("PageDown"); await visible();
      assert.ok(await popup.evaluate(element => element.scrollTop > 0), "keyboard users can page through long help");
      await fixtureHelp.press("End");
      assert.ok(await popup.evaluate(element => element.scrollTop + element.clientHeight >= element.scrollHeight - 1));
      await fixtureHelp.press("Home");
      assert.equal(await popup.evaluate(element => element.scrollTop), 0);
      assert.deepEqual(await frame.evaluate(() => ({ x: window.scrollX, y: window.scrollY })), pageScroll, "help scrolling leaves the page stationary");
      await popup.evaluate(element => { element.scrollTop = 100; element.dispatchEvent(new Event("scroll")); });
      await visible();
      assert.ok(await popup.evaluate(element => element.scrollTop > 0));
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${preference}-long.png`) });
      await page.keyboard.press("Escape"); await hidden();
    }
    await frame.evaluate(() => document.getElementById("tooltipFixture").remove());
    await frame.evaluate(() => window.ImageStudioAppearance.discard());

    // Model defaults and tool configuration share the same readable labels.
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-settings-provider="tooltip-fixture"]').click();
    await frame.locator('[data-settings-model="tooltip-fixture"]').click();
    await help(frame.locator('[data-schema-default="count"]'), "生图张数", "n", schema.count.description);
    await frame.locator('[data-edit-schema-policy="count"]').click();
    await frame.locator("#studioModalRoot").waitFor({ state: "visible" });
    const modalClose = frame.locator("#studioModalClose");
    await modalClose.focus(); await hidden();
    await modalClose.press("Tab"); await page.keyboard.press("Shift+Tab"); await visible();
    const onTop = await popup.evaluate(element => { const rect = element.getBoundingClientRect(); return element.contains(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)); });
    assert.equal(onTop, true, "hint is above the active modal scrim");
    await page.keyboard.press("Escape"); await hidden();
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true, "Escape dismisses the hint before closing its dialog");
    await modalClose.click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator('[data-model-tab="tool"]').click();
    const toolRow = frame.locator(".tool-parameter-row").filter({ has: frame.locator('[data-edit-tool-parameter="count"]') });
    assert.equal((await toolRow.locator("strong").innerText()).trim(), "生图张数");
    const toolHelp = toolRow.locator(".parameter-help-text");
    assert.match(await toolHelp.getAttribute("data-tooltip"), /^n\n/);
    assert.ok((await toolHelp.getAttribute("data-tooltip")).includes("工具张数说明。"));

    await frame.locator('[data-view="generate"]').click();
    await choose(frame, "#modelChoice", "tooltip-official:nai-diffusion-4-5-full");
    const officialModel = bootstrap.novelai_models.find(model => model.id === "nai-diffusion-4-5-full");
    await help(frame.locator('[data-model-parameter="characters"]'), officialModel.parameters.characters.label, officialModel.parameters.characters.request_key || "characters", officialModel.parameters.characters.description);
    await frame.locator('[data-mode="img2img"]').click();
    await choose(frame, "#modelChoice", "tooltip-official:nai-diffusion-4-5-full");
    await help(frame.locator('[data-model-parameter="reference_mode"]'), officialModel.parameters.reference_mode.label, officialModel.parameters.reference_mode.request_key || "reference_mode", officialModel.parameters.reference_mode.description);
    for (const view of ["generate", "gallery", "import", "settings"]) {
      await frame.locator(`[data-view="${view}"]`).click();
      await frame.waitForFunction(() => !document.querySelector("[title]"));
    }
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator("[data-detail-image]").click();
    if (width <= 540) {
      await frame.locator(".pswp--open").waitFor({ state: "visible" });
      await frame.waitForFunction(() => !document.querySelector(".pswp [title]"));
      assert.equal(await frame.locator(".pswp__button--image-studio-download").getAttribute("data-tooltip"), "下载图片", "dynamically created PhotoSwipe controls use themed hints");
      await page.keyboard.press("Escape");
      await frame.locator(".pswp--open").waitFor({ state: "detached" });
    } else {
      await frame.locator("#imagePreview").waitFor({ state: "visible" });
      assert.ok(await frame.locator("#closeImagePreview").getAttribute("data-tooltip"));
      await frame.locator("#closeImagePreview").click();
    }
    await frame.locator("#closeDrawer").click();
    const overflow = await frame.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `${engine} ${width}: horizontal overflow ${overflow}`);
    assert.deepEqual(errors, []);
    console.log(`PASS ${engine} ${width}: schema labels, desktop/touch hints, safe actions, ARIA, glass, long text, dynamic titles and dialogs`);
  } finally { await context.close(); }
}
(async () => {
  for (const engine of ["chromium", "webkit"]) {
    if (process.env.STUDIO_BROWSER && process.env.STUDIO_BROWSER !== engine) continue;
    const browser = await playwright[engine].launch({ headless: true });
    const widths = process.env.STUDIO_TEST_WIDTHS ? process.env.STUDIO_TEST_WIDTHS.split(",").map(Number) : engine === "chromium" ? [1440, 720, 390, 320] : [390];
    try { for (const width of widths) await run(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
