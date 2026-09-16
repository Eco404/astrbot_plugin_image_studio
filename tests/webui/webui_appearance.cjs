/* Run only against an isolated tests/support/webui_harness.py instance. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const browsers = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-appearance-"));
const storageKey = "image-studio:appearance:v1";
const defaults = { preference: "system", accentHue: 168, accentSaturation: 38, accentLightness: 50, glassOpacity: 0.68 };

async function open(context) {
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(base);
  const frame = page.frames().find((candidate) => candidate.url().includes("/ui/"));
  assert.ok(frame, "missing sandboxed iframe");
  await frame.waitForFunction(() => !!window.ImageStudioAppearance);
  await frame.evaluate(() => window.ImageStudioAppearance.ready);
  await frame.locator('[data-view="settings"]').click();
  await frame.locator("#appearanceSettings").scrollIntoViewIfNeeded();
  await frame.locator('[name="appearanceMode"]').first().waitFor({ state: "attached" });
  return { page, frame, errors };
}

async function theme(frame, value) {
  await frame.locator(`[name="appearanceMode"][value="${value}"] + span`).click();
  await frame.evaluate(() => window.ImageStudioAppearance.saved());
  await frame.waitForFunction((value) => document.documentElement.dataset.themePreference === value, value);
}

async function silentStatus(frame) {
  const status = frame.locator(".appearance-status");
  assert.equal((await status.textContent()).trim(), "", "normal theme saving must remain silent");
  assert.equal(await status.isVisible(), false, "empty theme status should be hidden");
  assert.equal(await status.evaluate((element) => element.getBoundingClientRect().height), 0, "empty theme status reserves layout space");
}

async function geometry(frame) {
  const value = await frame.evaluate(() => ({ view: document.documentElement.clientWidth, width: document.documentElement.scrollWidth }));
  assert.ok(value.width <= value.view + 1, `horizontal overflow: ${JSON.stringify(value)}`);
  for (const selector of [".appearance-modes", ".appearance-swatches", ".appearance-range"]) {
    const fits = await frame.locator(selector).first().evaluate((element) => element.scrollWidth <= element.clientWidth + 1);
    assert.ok(fits, `${selector} overflow`);
  }
}

async function assertMode(frame, resolved, preference) {
  assert.deepEqual(await frame.evaluate(() => ({ theme: document.documentElement.dataset.theme, preference: document.documentElement.dataset.themePreference, scheme: document.documentElement.style.colorScheme })), { theme: resolved, preference, scheme: resolved });
}

async function accentContrast(frame) {
  const values = await frame.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1;
    const context = canvas.getContext("2d");
    const sample = document.createElement("span"); sample.style.setProperty("transition", "none", "important"); document.body.appendChild(sample);
    const rgb = (token, backdrop = []) => {
      context.clearRect(0, 0, 1, 1);
      for (const layer of [...backdrop, token]) { sample.style.backgroundColor = `var(${layer})`; context.fillStyle = getComputedStyle(sample).backgroundColor; context.fillRect(0, 0, 1, 1); }
      return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const luma = (values) => values.map((v) => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }).reduce((sum, v, index) => sum + v * [0.2126, 0.7152, 0.0722][index], 0);
    const contrast = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    const original = window.ImageStudioAppearance.get();
    let smallest = Infinity, controlMinimum = Infinity;
    const mismatches = [];
    for (const hue of [0, 17, 57, 130, 168, 195, 216, 279, 345, 359]) for (const saturation of [0, 100]) for (const lightness of [0, 7, 50, 94, 100]) {
      window.ImageStudioAppearance.set({ accentHue: hue, accentSaturation: saturation, accentLightness: lightness }, false);
      smallest = Math.min(smallest, contrast(luma(rgb("--accent")), luma(rgb("--on-accent"))), contrast(luma(rgb("--selection-fill", ["--page", "--glass"])), luma(rgb("--accent-strong"))));
      controlMinimum = Math.min(controlMinimum,
        contrast(luma(rgb("--control-accent")), luma(rgb("--on-control-accent"))),
        contrast(luma(rgb("--control-accent-hover")), luma(rgb("--on-control-accent-hover"))));
      const sourceHex = document.getElementById("appearanceColor").value;
      const expected = sourceHex.slice(1).match(/../g).map((part) => parseInt(part, 16));
      for (const token of ["--control-accent", "--control-accent-hover"]) {
        const rendered = rgb(token);
        if (rendered.some((value, index) => value !== expected[index]) && mismatches.length < 5) mismatches.push({ hue, saturation, lightness, sourceHex, token, rendered });
      }
    }
    window.ImageStudioAppearance.set(original);
    sample.remove();
    return { smallest, controlMinimum, mismatches };
  });
  assert.ok(values.smallest >= 4.5, `accent text contrast below 4.5:1: ${values.smallest}`);
  assert.ok(values.controlMinimum >= 4.5, `control fill contrast below 4.5:1: ${values.controlMinimum}`);
  assert.deepEqual(values.mismatches, [], "image-selection accent tokens must preserve the selected source color");
}

async function selectionCompositing(frame) {
  const samples = await frame.evaluate(() => {
    const saved = window.ImageStudioAppearance.get();
    const probe = document.createElement("span"); probe.style.setProperty("transition", "none", "important"); document.body.appendChild(probe);
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    const token = (name) => { probe.style.backgroundColor = `var(${name})`; return getComputedStyle(probe).backgroundColor; };
    const paint = (layers) => {
      context.clearRect(0, 0, 1, 1);
      for (const color of layers) { context.fillStyle = color; context.fillRect(0, 0, 1, 1); }
      return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const delta = (a, b) => Math.max(...a.map((channel, index) => Math.abs(channel - b[index])));
    const result = [];
    for (const preference of ["light", "dark"]) for (const glassOpacity of [.2, .68, 1]) {
      window.ImageStudioAppearance.set({ preference, glassOpacity, accentHue: 345, accentSaturation: 40, accentLightness: 50 }, false);
      const page = token("--page"), glass = token("--glass"), tint = token("--selection-fill"), oldSurface = paint([token("--accent-soft")]);
      // Use the actual browser-resolved CSS layers, without reproducing the
      // implementation's tint compensation calculation.
      const surface = paint([page, glass, tint]);
      const lowBackdrop = paint(["#165569", glass, tint]), highBackdrop = paint(["#ad8375", glass, tint]);
      result.push({ preference, glassOpacity, oldSurface, surface, targetDelta: delta(oldSurface, surface), backdropDelta: delta(lowBackdrop, highBackdrop) });
    }
    window.ImageStudioAppearance.set(saved, false); probe.remove();
    return result;
  });
  for (const sample of samples) {
    assert.ok(sample.targetDelta <= 8, `selection must remain close to the old palette on the normal page/glass backdrop: ${JSON.stringify(sample)}`);
    if (sample.glassOpacity < 1) assert.ok(sample.backdropDelta >= 5, `changed backdrop should show through the selected surface: ${JSON.stringify(sample)}`);
    else assert.equal(sample.backdropDelta, 0, "opaque enclosing glass must correctly conceal the page below it");
  }
}

async function sidebarControls(browser, engine, width = 1440) {
  const context = await browser.newContext({ viewport: { width, height: width < 540 ? 844 : 1000 } });
  const { page, frame, errors } = await open(context);
  async function checkedThumb() {
    await frame.waitForFunction(() => {
      const input = document.getElementById("settingTool"), thumb = getComputedStyle(input.nextElementSibling, "::after");
      const expected = document.documentElement.dataset.theme === "dark" ? getComputedStyle(document.querySelector(".provider-row span")).color : "rgb(255, 255, 255)";
      return input.checked && thumb.backgroundColor === expected && new DOMMatrix(thumb.transform).m41 === 16;
    });
  }
  try {
    await selectionCompositing(frame);
    for (const [mode, sourceHex] of ["light", "dark"].flatMap((mode) => ["#dcc1cf", "#0066ff", "#000000", "#ffffff"].map((hex) => [mode, hex]))) {
      await theme(frame, mode);
      await frame.locator("#appearanceHex").fill(sourceHex);
      await frame.locator("#appearanceHex").press("Enter");
      await frame.evaluate(() => window.ImageStudioAppearance.saved());
      await silentStatus(frame);
      assert.equal((await frame.locator("#appearanceColor").inputValue()).toLowerCase(), sourceHex);
      // The save button intentionally turns yellow for an unsaved draft. Use a
      // normal primary action to compare the actual navigation/control surfaces.
      const button = frame.locator("#addProviderButton");
      await page.mouse.move(0, 0);
      for (const hovered of [false, true]) {
        if (hovered) await button.hover();
        const rendering = await frame.waitForFunction(() => {
          const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
          const context = canvas.getContext("2d");
          const rgba = (color) => { context.clearRect(0, 0, 1, 1); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return [...context.getImageData(0, 0, 1, 1).data]; };
          const rgb = (color) => rgba(color).slice(0, 3);
          const composed = (element) => {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--page"); context.fillRect(0, 0, 1, 1);
            const ancestors = []; for (let node = element; node; node = node.parentElement) ancestors.unshift(node);
            for (const node of ancestors) { context.fillStyle = getComputedStyle(node).backgroundColor; context.fillRect(0, 0, 1, 1); }
            return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
          };
          const luma = (values) => values.map((v) => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }).reduce((sum, v, index) => sum + v * [0.2126, 0.7152, 0.0722][index], 0);
          const button = document.getElementById("addProviderButton");
          const nav = document.querySelector(".nav-item.is-active");
          const navSurface = innerWidth <= 900 ? nav.querySelector(".nav-icon") : nav;
          const navigationFill = rgba(getComputedStyle(navSurface).backgroundColor);
          const navigationText = rgba(getComputedStyle(nav).color);
          const selectedSwitch = document.querySelector(".toggle-control input:checked + span");
          if (!selectedSwitch) return false;
          const fill = rgba(getComputedStyle(button).backgroundColor), foreground = rgba(getComputedStyle(button).color);
          const switchFill = rgba(getComputedStyle(selectedSwitch).backgroundColor), switchForeground = rgb(getComputedStyle(selectedSwitch, "::after").backgroundColor);
          const expectedThumb = document.documentElement.dataset.theme === "dark" ? rgb(getComputedStyle(document.querySelector(".provider-row span")).color) : [255, 255, 255];
          const contrast = (a, b) => (Math.max(luma(a), luma(b)) + .05) / (Math.min(luma(a), luma(b)) + .05);
          const ratio = contrast(composed(button), foreground.slice(0, 3));
          if (fill.some((value, index) => value !== navigationFill[index]) || foreground.some((value, index) => value !== navigationText[index]) || switchFill.some((value, index) => value !== navigationFill[index]) || ratio < 4.5 || switchForeground.some((value, index) => value !== expectedThumb[index])) return false;
          return { fill, foreground, switchFill, ratio, switchForeground, navigationFill, navigationText, expectedThumb, buttonOpacity: getComputedStyle(button).opacity, navOpacity: getComputedStyle(navSurface).opacity, navOuterAlpha: rgba(getComputedStyle(nav).backgroundColor)[3] };
        }, null, { timeout: 5000 });
        const value = await rendering.jsonValue();
        assert.deepEqual(value.fill, value.navigationFill, `${mode} ${sourceHex} ${hovered ? "hover" : "normal"} button must match the selected navigation surface`);
        assert.deepEqual(value.foreground, value.navigationText, `${mode} ${sourceHex} button text must match selected navigation text`);
        assert.deepEqual(value.switchFill, value.navigationFill, `${mode} ${sourceHex} switch must match the selected navigation surface`);
        assert.deepEqual(value.switchForeground, value.expectedThumb, `${mode} enabled switch thumb uses white in light mode and the muted palette in dark mode`);
        assert.ok(value.fill[3] > 128 && value.fill[3] < 217, `${mode} ${sourceHex}: selection tint must be translucent`);
        assert.equal(value.foreground[3], 255, "button and selected navigation text must remain opaque");
        assert.equal(value.buttonOpacity, "1", "do not fade the whole button to make its background translucent");
        assert.equal(value.navOpacity, "1", "do not fade the whole selected navigation surface");
        if (width <= 900) assert.equal(value.navOuterAlpha, 0, "mobile navigation must retain a transparent outer row");
      }
      await button.evaluate((element) => { element.disabled = true; });
      const disabled = await button.evaluate(async (element) => {
        const nav = document.querySelector(".nav-item.is-active");
        const navSurface = innerWidth <= 900 ? nav.querySelector(".nav-icon") : nav;
        await Promise.all([...element.getAnimations(), ...nav.getAnimations({ subtree: true })].map((animation) => animation.finished.catch(() => {})));
        return { fill: getComputedStyle(element).backgroundColor, text: getComputedStyle(element).color, navFill: getComputedStyle(navSurface).backgroundColor, navText: getComputedStyle(nav).color };
      });
      assert.equal(disabled.fill, disabled.navFill, `${mode}/${sourceHex}: disabled primary retains the navigation fill`);
      assert.equal(disabled.text, disabled.navText, `${mode}/${sourceHex}: disabled primary retains the navigation text`);
      await button.evaluate((element) => { element.disabled = false; });
      for (const list of ["#settingsProviderList", "#settingsModelList"]) {
        const active = frame.locator(`${list} .provider-row.is-active`);
        await active.waitFor();
        await active.hover();
        await active.evaluate(async (element) => {
          await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => {})));
        });
        const border = await active.evaluate((element) => {
          const style = getComputedStyle(element); const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
          const context = canvas.getContext("2d"); context.fillStyle = style.borderTopColor; context.fillRect(0, 0, 1, 1);
          return { alpha: context.getImageData(0, 0, 1, 1).data[3], width: parseFloat(style.borderTopWidth) };
        });
        assert.ok(border.alpha === 0 || border.width === 0, `${mode} ${sourceHex} ${list}: selected row still has a decorative border`);
      }
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${mode}-${sourceHex.slice(1)}-sidebar-controls.png`), animations: "disabled" });
      const toggle = frame.locator("#settingTool");
      await toggle.locator("+ span").click();
      await frame.waitForFunction(() => {
        const input = document.getElementById("settingTool"), thumb = getComputedStyle(input.nextElementSibling, "::after");
        const sample = document.createElement("span"); sample.style.color = "var(--muted)"; document.body.appendChild(sample);
        const muted = getComputedStyle(sample).color; sample.remove();
        return !input.checked && thumb.backgroundColor === muted && (thumb.transform === "none" || new DOMMatrix(thumb.transform).m41 === 0);
      });
      await toggle.locator("+ span").click();
      await checkedThumb();
    }
    // Theme changes also update an already-enabled switch, including returning
    // from dark mode to light without first toggling the control off and on.
    for (const mode of ["light", "dark", "light"]) {
      await theme(frame, mode);
      await checkedThumb();
      await frame.locator("#settingTool").locator("+ span").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${mode}-switch-theme-restored.png`), animations: "disabled" });
    }
    assert.deepEqual(errors, []);
    console.log(`${engine} ${width}px: translucent navigation/buttons/switches preserve the palette and text contrast over composed glass, background changes show through, mobile outer rows stay transparent, and selected settings rows remain borderless`);
  } finally { await context.close(); }
}

async function sandboxMatrix(browser, engine) {
  for (const [width, height] of [[1440, 1000], [1100, 900], [900, 1000], [720, 1212], [390, 844], [320, 700]]) {
    const context = await browser.newContext({ viewport: { width, height }, colorScheme: "light", reducedMotion: width === 320 ? "reduce" : "no-preference", hasTouch: width < 720 });
    await context.addInitScript(() => {
      window.__appearancePaints = [];
      function sample() {
        window.__appearancePaints.push({ theme: document.documentElement.dataset.theme, visible: document.body && getComputedStyle(document.body).visibility });
        if (window.__appearancePaints.length < 30) requestAnimationFrame(sample);
      }
      requestAnimationFrame(sample);
    });
    const { page, frame, errors } = await open(context);
    assert.equal(await frame.evaluate(() => { try { return !!localStorage; } catch { return false; } }), false, "harness must retain opaque sandbox");
    await assertMode(frame, "light", "system");
    await page.emulateMedia({ colorScheme: "dark" });
    await frame.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await assertMode(frame, "dark", "system");
    await theme(frame, "light");
    await assertMode(frame, "light", "light");
    await frame.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
    await assertMode(frame, "light", "light");
    for (const mode of ["light", "dark"]) {
      await theme(frame, mode);
      await accentContrast(frame);
      const before = await frame.evaluate(() => { const style = getComputedStyle(document.documentElement); return [style.getPropertyValue("--danger"), style.getPropertyValue("--success"), style.getPropertyValue("--warning")]; });
      await frame.locator('[name="appearanceAccent"][value="345"] + span').click();
      for (const extremes of [{ accentSaturation: 0, glassOpacity: 0.2, accentLightness: 0 }, { accentSaturation: 100, glassOpacity: 1, accentLightness: 100 }]) {
        await frame.evaluate((values) => window.ImageStudioAppearance.set(values), extremes);
        await frame.evaluate(() => window.ImageStudioAppearance.saved());
        const after = await frame.evaluate(() => { const style = getComputedStyle(document.documentElement); return [style.getPropertyValue("--danger"), style.getPropertyValue("--success"), style.getPropertyValue("--warning")]; });
        assert.deepEqual(after, before, "semantic status colors changed with accent");
        await geometry(frame);
        await frame.locator("#appearanceSettings").screenshot({ path: path.join(output, `${engine}-${width}-${mode}-${extremes.accentSaturation}-card.png`) });
      }
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${mode}-viewport.png`) });
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${mode}-full.png`), fullPage: true });
    }
    const cookies = await context.cookies();
    const cookie = cookies.find((item) => item.name === "image_studio_appearance_v1");
    assert.ok(cookie && cookie.httpOnly && cookie.sameSite === "Lax", "opaque iframe theme was not saved in browser cookies");
    await silentStatus(frame);
    await theme(frame, "light");
    const saved = await frame.evaluate(() => window.ImageStudioAppearance.get());
    await page.reload();
    const reopened = page.frames().find((candidate) => candidate.url().includes("/ui/"));
    await reopened.waitForFunction(() => !!window.ImageStudioAppearance);
    await reopened.evaluate(() => window.ImageStudioAppearance.ready);
    assert.deepEqual(await reopened.evaluate(() => window.ImageStudioAppearance.get()), saved);
    assert.ok((await reopened.evaluate(() => window.__appearancePaints)).every((sample) => !sample.visible || sample.visible === "hidden" || sample.theme === "light"), "wrong cookie theme painted before initialization");
    await reopened.locator('[data-view="settings"]').click();
    await reopened.locator("#appearanceSettings").scrollIntoViewIfNeeded();
    await reopened.locator('[name="appearanceMode"][value="dark"]').focus();
    await page.keyboard.press("ArrowLeft");
    assert.equal(await reopened.locator('[name="appearanceMode"][value="light"]').isChecked(), true);
    await reopened.locator('[name="appearanceAccent"][value="345"]').focus();
    await page.keyboard.press("ArrowLeft");
    assert.equal(await reopened.locator('[name="appearanceAccent"][value="216"]').isChecked(), true);
    const focusedStyle = await reopened.locator('[name="appearanceAccent"][value="216"] + span').evaluate((element) => getComputedStyle(element).outlineStyle);
    assert.notEqual(focusedStyle, "none", "keyboard focus not visible");
    await reopened.locator(".appearance-reset").click();
    await page.keyboard.press("Escape");
    assert.equal(await reopened.locator(".appearance-reset-confirmation").isVisible(), false);
    await reopened.locator(".appearance-reset").click();
    await reopened.locator('[data-appearance-reset="confirm"]').click();
    assert.deepEqual(await reopened.evaluate(() => window.ImageStudioAppearance.get()), defaults);
    await reopened.evaluate(() => window.ImageStudioAppearance.saved());
    assert.equal(await reopened.locator("#saveSettingsButton").evaluate((element) => element.classList.contains("is-dirty")), false, "appearance marked plugin config dirty");
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`${engine} ${width}px: sandbox cookie, system/light/dark, bounds, reset, keyboard, layout passed`);
  }
  // A 1440px display at 200% zoom has a 720 CSS-pixel layout viewport.
  const zoomContext = await browser.newContext({ viewport: { width: 720, height: 500 }, deviceScaleFactor: 2 });
  const zoomed = await open(zoomContext);
  await geometry(zoomed.frame);
  await zoomed.page.screenshot({ path: path.join(output, `${engine}-zoom200-layout-equivalent.png`) });
  await zoomContext.close();
}

async function localAndFailure(browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  await context.route(`${base.replace(/\/$/, "")}/appearance-check`, (route) => route.fulfill({ contentType: "text/html", body: `<html><head><script src="/ui/appearance.js"></script><link rel="stylesheet" href="/ui/app.css"><link rel="stylesheet" href="/ui/appearance.css"></head><body><section id="appearanceSettings"></section></body></html>` }));
  await context.addInitScript((key) => {
    if (!sessionStorage.getItem("appearance-seeded")) {
      localStorage.setItem(key, JSON.stringify({ preference: "light", accentHue: 9999, accentSaturation: "red", glassOpacity: -10 }));
      sessionStorage.setItem("appearance-seeded", "true");
    }
    window.__appearancePaints = [];
    function sample() { window.__appearancePaints.push({ theme: document.documentElement.dataset.theme, pending: document.documentElement.dataset.appearancePending, visible: document.body && getComputedStyle(document.body).visibility }); if (window.__appearancePaints.length < 12) requestAnimationFrame(sample); }
    requestAnimationFrame(sample);
  }, storageKey);
  const page = await context.newPage();
  await page.goto(`${base.replace(/\/$/, "")}/appearance-check`);
  await page.evaluate(() => window.ImageStudioAppearance.ready);
  assert.deepEqual(await page.evaluate(() => window.ImageStudioAppearance.get()), { preference: "light", accentHue: 359.999999, accentSaturation: 38, accentLightness: 50, glassOpacity: 0.2 });
  assert.ok((await page.evaluate(() => window.__appearancePaints)).every((frame) => !frame.visible || frame.visible === "hidden" || frame.theme === "light"), "wrong local theme painted");
  await page.evaluate(() => window.ImageStudioAppearance.set({ preference: "dark" }));
  await silentStatus(page);
  await page.reload();
  await page.evaluate(() => window.ImageStudioAppearance.ready);
  await assertMode(page, "dark", "dark");
  await page.evaluate((key) => localStorage.setItem(key, "{broken"), storageKey);
  await page.reload();
  await page.evaluate(() => window.ImageStudioAppearance.ready);
  assert.deepEqual(await page.evaluate(() => window.ImageStudioAppearance.get()), defaults);
  await context.close();

  const fallback = await browser.newContext();
  await fallback.route("**/astrbot_plugin_image_studio/appearance", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "主题服务暂不可用" }) }));
  const opened = await open(fallback);
  assert.equal(await opened.frame.locator("body").evaluate((element) => getComputedStyle(element).visibility), "visible", "failed cookie read left application hidden");
  assert.match(await opened.frame.locator(".appearance-status").textContent(), /未能读取/);
  assert.equal(await opened.frame.locator(".appearance-status").isVisible(), true);
  await theme(opened.frame, "dark");
  assert.match(await opened.frame.locator(".appearance-status").textContent(), /未保存/);
  assert.equal(await opened.frame.locator(".appearance-status").isVisible(), true);
  assert.deepEqual(opened.errors, []);
  await fallback.close();
  console.log("local prepaint/persistence/malformed storage and cookie failure recovery passed");
}

async function silentPersistence(browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const { page, frame, errors } = await open(context);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const postStarted = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/appearance"), { timeout: 10000 });
  await context.route("**/astrbot_plugin_image_studio/appearance", async (route) => {
    if (route.request().method() === "POST") await gate;
    await route.continue();
  });
  try {
    await silentStatus(frame);
    await frame.locator('[name="appearanceMode"][value="dark"] + span').click();
    await postStarted;
    await silentStatus(frame);
    await assertMode(frame, "dark", "dark");
    release();
    await frame.evaluate(() => window.ImageStudioAppearance.saved());
    await silentStatus(frame);
    const actual = await frame.evaluate(() => window.AstrBotPluginPage.apiGet("appearance"));
    assert.deepEqual(actual, await frame.evaluate(() => window.ImageStudioAppearance.get()), "silent save did not persist the browser preference");
    const cookies = await context.cookies();
    assert.ok(cookies.some((cookie) => cookie.name === "image_studio_appearance_v1"), "silent save did not set browser cookie");
    assert.deepEqual(errors, []);
    console.log("delayed cookie POST remains silent during saving and after verified persistence");
  } finally { release(); await page.close(); await context.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSERS || "chromium").split(",")) {
    const browser = await browsers[engine].launch({ headless: true });
    try {
      if (process.env.STUDIO_APPEARANCE_EXACT_ONLY !== "1") {
        await sandboxMatrix(browser, engine); await localAndFailure(browser); await silentPersistence(browser);
      }
      for (const width of [1440, 390]) await sidebarControls(browser, engine, width);
    }
    finally { await browser.close(); }
  }
  console.log(`Appearance screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
