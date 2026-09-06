/* Run only against an isolated tests/webui_harness.py instance. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const browsers = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-color-picker-"));

async function open(browser, options = {}) {
  const context = await browser.newContext({ viewport: { width: options.width || 390, height: 844 }, colorScheme: options.theme || "light", hasTouch: (options.width || 390) < 720 });
  await context.addInitScript((native) => {
    window.__eyeDropperMode = "success";
    window.__eyeDropperCalls = 0;
    if (native) Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(window, "EyeDropper", { configurable: true, writable: true, value: native ? class {
      async open() {
        window.__eyeDropperCalls += 1;
        if (window.__eyeDropperMode === "abort") throw new DOMException("Cancelled", "AbortError");
        if (window.__eyeDropperMode === "error") throw new DOMException("Permission denied", "SecurityError");
        return { sRGBHex: "#2165c7" };
      }
    } : undefined });
  }, !!options.native);
  const page = await context.newPage();
  const errors = [];
  const uploads = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.method() === "POST" && !request.url().endsWith("/appearance")) uploads.push(request.url());
  });
  await page.goto(base);
  const frame = page.frames().find((candidate) => candidate.url().includes("/ui/"));
  assert.ok(frame);
  await frame.waitForFunction(() => !!window.ImageStudioAppearance);
  await frame.evaluate(() => window.ImageStudioAppearance.ready);
  await frame.locator('[data-view="settings"]').click();
  await frame.locator("#appearanceSettings").scrollIntoViewIfNeeded();
  await frame.locator("#appearanceColor").waitFor();
  return { context, page, frame, errors, uploads };
}

async function saved(frame) {
  await frame.evaluate(() => window.ImageStudioAppearance.saved());
  assert.equal((await frame.locator(".appearance-status").textContent()).trim(), "");
  assert.equal(await frame.locator(".appearance-status").isVisible(), false);
  assert.equal(await frame.locator(".appearance-status").evaluate((element) => element.getBoundingClientRect().height), 0);
  const actual = await frame.evaluate(() => window.AstrBotPluginPage.apiGet("appearance"));
  assert.deepEqual(actual, await current(frame), "color settings were not actually persisted");
}

async function current(frame) { return frame.evaluate(() => window.ImageStudioAppearance.get()); }

async function expectHex(frame, hex) {
  const expected = hex.toLowerCase();
  await frame.waitForFunction((expected) => document.getElementById("appearanceColor").value.toLowerCase() === expected, expected);
  assert.equal((await frame.locator("#appearanceHex").inputValue()).toLowerCase(), expected);
}

async function geometry(frame) {
  assert.ok(await frame.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), "page overflow");
  for (const selector of ["#appearanceSettings", "#appearanceSampler"]) {
    if (await frame.locator(selector).isVisible()) assert.ok(await frame.locator(selector).evaluate((node) => node.scrollWidth <= node.clientWidth + 1), `${selector} overflow`);
  }
}

async function fixture(frame) {
  const url = await frame.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 80; canvas.height = 60;
    const context = canvas.getContext("2d");
    context.fillStyle = "#20a060"; context.fillRect(0, 0, 40, 60);
    context.fillStyle = "#d04080"; context.fillRect(40, 0, 40, 60);
    return canvas.toDataURL("image/png");
  });
  return { name: "theme-local-sample.png", mimeType: "image/png", buffer: Buffer.from(url.split(",")[1], "base64") };
}

async function uploadFixture(frame, file) {
  await frame.locator("#appearanceSampleFile").setInputFiles(file);
  await frame.locator("#appearanceSampleCanvas").waitFor();
  await frame.waitForFunction(() => { const canvas = document.getElementById("appearanceSampleCanvas"); return canvas.width > 0 && canvas.height > 0 && !document.getElementById("appearanceSampler").hidden; });
}

async function pickCanvas(frame, side) {
  const canvas = frame.locator("#appearanceSampleCanvas");
  const box = await canvas.boundingBox();
  assert.ok(box);
  await canvas.click({ position: { x: box.width * (side === "left" ? 0.25 : 0.75), y: box.height * 0.5 } });
}

async function inputAndNative(browser, engine) {
  const { context, page, frame, errors, uploads } = await open(browser, { width: 1440, native: true });
  try {
    await frame.locator("#appearanceHex").fill("#f80");
    await frame.locator("#appearanceHex").press("Enter");
    await expectHex(frame, "#ff8800");
    await saved(frame);
    const orange = await current(frame);
    assert.ok(Math.abs(orange.accentHue - 32) < 0.6 && orange.accentSaturation === 100 && Math.abs(orange.accentLightness - 50) < 0.6);
    await frame.locator("#appearanceHex").fill("#1476b8");
    await frame.locator("#appearanceHex").press("Tab");
    await expectHex(frame, "#1476b8");
    const selected = await current(frame);
    await frame.locator("#appearanceHex").fill("#zzzzzz");
    await frame.locator("#appearanceHex").press("Enter");
    assert.deepEqual(await current(frame), selected, "invalid HEX changed stored theme");
    const invalid = await frame.locator("#appearanceHex").evaluate((element) => element.getAttribute("aria-invalid") === "true" || !element.validity.valid);
    const message = await frame.locator(".appearance-status").textContent();
    assert.ok(invalid || /错误|格式|有效|十六|HEX/.test(message), "invalid HEX not explained");
    await frame.locator("#appearanceHex").fill("#1476b8");
    await frame.locator("#appearanceHex").press("Enter");
    await frame.locator("#appearanceColor").focus();
    await frame.locator("#appearanceColor").evaluate((input) => {
      input.value = "#713ad1";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expectHex(frame, "#713ad1");
    await saved(frame);
    const persisted = await current(frame);
    await page.reload();
    const resumed = page.frames().find((candidate) => candidate.url().includes("/ui/"));
    await resumed.waitForFunction(() => !!window.ImageStudioAppearance);
    await resumed.evaluate(() => window.ImageStudioAppearance.ready);
    assert.deepEqual(await current(resumed), persisted, "custom HSL did not survive cookie reload");
    await resumed.locator('[data-view="settings"]').click();
    await expectHex(resumed, "#713ad1");
    await resumed.locator("#appearanceEyedropper").click();
    await expectHex(resumed, "#2165c7");
    assert.equal(await resumed.evaluate(() => window.__eyeDropperCalls), 1);
    await saved(resumed);
    const beforeAbort = await current(resumed);
    await resumed.evaluate(() => { window.__eyeDropperMode = "abort"; });
    await resumed.locator("#appearanceEyedropper").click();
    assert.deepEqual(await current(resumed), beforeAbort, "native eyedropper cancellation changed color");
    assert.equal(await resumed.locator("#appearanceSampler").isVisible(), false, "native cancellation unexpectedly opened fallback");
    await resumed.evaluate(() => { window.__eyeDropperMode = "error"; });
    await resumed.locator("#appearanceEyedropper").click();
    await resumed.locator("#appearanceSampler").waitFor();
    assert.deepEqual(await current(resumed), beforeAbort, "permission failure changed color");
    await resumed.locator("#appearanceSampleClose").click();
    await geometry(resumed);
    assert.deepEqual(uploads, [], "color sampling uploaded file data");
    assert.deepEqual(errors, []);
    console.log(`${engine}: custom HEX, color input, HSL persistence, native eyedropper success/abort/error passed`);
  } finally { await context.close(); }
}

async function localSampler(browser, engine) {
  for (const width of [1440, 541, 540, 390, 320]) for (const theme of ["light", "dark"]) {
    const { context, page, frame, errors, uploads } = await open(browser, { width, theme });
    try {
      const initial = await current(frame);
      if (width <= 540) {
        assert.equal(await frame.locator("#appearanceEyedropper").isVisible(), false, "mobile eyedropper must be hidden");
        assert.equal(await frame.locator("#appearanceColor").isVisible(), true);
        assert.equal(await frame.locator("#appearanceHex").isVisible(), true);
        const tracks = await frame.locator(".appearance-custom-color").evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(" ").length);
        assert.equal(tracks, 2, "hidden eyedropper must not reserve a grid column");
        await frame.locator("#appearanceHex").fill("#dcc1cf");
        await frame.locator("#appearanceHex").press("Enter");
        await expectHex(frame, "#dcc1cf"); await saved(frame);
        await frame.locator("#appearanceHex").press("Tab");
        assert.notEqual(await frame.evaluate(() => document.activeElement.id), "appearanceEyedropper");
        await frame.locator("#appearanceColor").focus();
        await frame.locator("#appearanceColor").evaluate((input) => {
          input.value = "#1476b8";
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        await expectHex(frame, "#1476b8"); await saved(frame);
        await geometry(frame);
        await page.screenshot({ path: path.join(output, `${engine}-${width}-${theme}-mobile-color.png`) });
        assert.deepEqual(uploads, []); assert.deepEqual(errors, []);
        console.log(`${engine} ${width}px ${theme}: hidden eyedropper, custom color/HEX, keyboard and layout passed`);
        continue;
      }
      await frame.locator("#appearanceEyedropper").click();
      await frame.locator("#appearanceSampler").waitFor();
      const file = await fixture(frame);
      await uploadFixture(frame, file);
      await pickCanvas(frame, "left");
      assert.deepEqual(await current(frame), initial, "sampling preview changed color before confirmation");
      await frame.locator("#appearanceSampleClose").click();
      assert.deepEqual(await current(frame), initial, "cancelled sampling changed color");
      await frame.locator("#appearanceEyedropper").focus();
      await page.keyboard.press("Enter");
      await frame.locator("#appearanceSampler").waitFor();
      // Exercise the themed file chooser without relying on an OS-owned picker.
      const choosing = page.waitForEvent("filechooser");
      await frame.locator("#appearanceSampleChoose").click();
      await (await choosing).setFiles(file);
      await frame.locator("#appearanceSampleCanvas").waitFor();
      await pickCanvas(frame, "left");
      await frame.locator("#appearanceSampleCanvas").focus();
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("Enter");
      await expectHex(frame, "#20a060");
      await saved(frame);
      assert.equal(await frame.locator("#appearanceSampler").isVisible(), false);
      await frame.locator("#appearanceEyedropper").click();
      await uploadFixture(frame, file);
      await pickCanvas(frame, "right");
      await geometry(frame);
      await page.screenshot({ path: path.join(output, `${engine}-${width}-${theme}-sampler.png`) });
      await frame.locator("#appearanceSampleApply").click();
      await expectHex(frame, "#d04080");
      await saved(frame);
      await frame.locator("#appearanceEyedropper").click();
      await frame.locator("#appearanceSampleClose").focus();
      await page.keyboard.press("Escape");
      assert.equal(await frame.locator("#appearanceSampler").isVisible(), false);
      if (width === 1440 && theme === "light") {
        await frame.locator("#appearanceEyedropper").click();
        await frame.locator("#appearanceSampleFile").setInputFiles({ name: "broken.png", mimeType: "image/png", buffer: Buffer.from("not-an-image") });
        await frame.locator("#appearanceSampleValue").filter({ hasText: "读取失败" }).waitFor();
        assert.equal(await frame.locator("#appearanceSampleApply").isDisabled(), true);
        await frame.evaluate(() => {
          const original = HTMLImageElement.prototype.decode;
          const gate = new Promise((resolve) => { window.__releaseColorDecode = resolve; });
          HTMLImageElement.prototype.decode = async function () {
            const decoded = await original.call(this);
            if (this.src.startsWith("blob:")) await gate;
            return decoded;
          };
          window.__restoreColorDecode = () => { HTMLImageElement.prototype.decode = original; };
        });
        await frame.locator("#appearanceSampleFile").setInputFiles(file);
        await frame.locator("#appearanceSampleValue").filter({ hasText: "正在读取" }).waitFor();
        await frame.locator("#appearanceSampleClose").click();
        await frame.evaluate(async () => { window.__releaseColorDecode(); window.__restoreColorDecode(); await new Promise((resolve) => requestAnimationFrame(resolve)); });
        assert.equal(await frame.locator("#appearanceSampler").isVisible(), false, "late image decode reopened cancelled sampler");
        assert.equal(await frame.locator("#appearanceSampleCanvas").evaluate((canvas) => canvas.width), 0, "late image decode restored cancelled pixels");
        await expectHex(frame, "#d04080");
      }
      if (width === 541) {
        await frame.locator("#appearanceEyedropper").click();
        await page.setViewportSize({ width: 540, height: 844 });
        assert.equal(await frame.locator("#appearanceEyedropper").isVisible(), false);
        await frame.locator("#appearanceSampleClose").click();
        assert.equal(await frame.evaluate(() => document.activeElement.id), "appearanceColor", "sampler dismissal must not focus a hidden mobile trigger");
      }
      await geometry(frame);
      assert.equal(await frame.locator("#saveSettingsButton").evaluate((element) => element.classList.contains("is-dirty")), false, "browser color preference dirtied plugin config");
      assert.deepEqual(uploads, [], "local sampling must never upload images");
      assert.deepEqual(errors, []);
      console.log(`${engine} ${width}px ${theme}: local-only sample, preview/confirm/cancel, file chooser, keyboard and layout passed`);
    } finally { await context.close(); }
  }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSERS || "chromium").split(",")) {
    const browser = await browsers[engine].launch({ headless: true });
    try { await inputAndNative(browser, engine); await localSampler(browser, engine); }
    finally { await browser.close(); }
  }
  console.log(`Color picker screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
