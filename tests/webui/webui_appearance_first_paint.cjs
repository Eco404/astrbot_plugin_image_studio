/* Run only against an isolated tests/support/webui_harness.py instance. */
const assert = require("node:assert/strict");
// WebKit resolves fonts.ready after parsing; these screenshots deliberately
// happen while app.js blocks parsing and the page has no visible text.
process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = "1";
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

const saved = { preference: "dark", accentHue: 345, accentSaturation: 65, accentLightness: 60, glassOpacity: .4 };
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

async function open(context) {
  const page = await context.newPage();
  assert.match(await (await page.request.get(base)).text(), /<iframe id="studio"/, "isolated harness required");
  await page.goto(base, { waitUntil: "commit" });
  await page.locator("#studio").waitFor();
  await page.evaluate(() => { document.body.style.background = "#58446f"; });
  const frame = await page.locator("#studio").elementHandle().then((element) => element.contentFrame());
  await frame.waitForFunction(() => !!window.__imageStudioAppearanceGate);
  return { page, frame };
}

async function sampleFrames(context) {
  await context.addInitScript(() => {
    window.__appearanceFrames = [];
    const sample = () => {
      if (document.body) {
        const root = document.documentElement, button = document.querySelector(".nav-item.is-active .nav-icon");
        window.__appearanceFrames.push({
          pending: root.dataset.appearancePending === "true",
          display: getComputedStyle(document.body).display,
          theme: root.dataset.theme,
          accent: root.style.getPropertyValue("--control-accent"),
          fill: button && getComputedStyle(button).backgroundColor,
          transitions: document.getAnimations().filter((animation) => animation instanceof CSSTransition).length,
        });
      }
      if (window.__appearanceFrames.length < 500) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
}

async function assertBlank(page, frame) {
  await frame.locator("body").waitFor({ state: "attached" });
  assert.deepEqual(await frame.evaluate(() => ({
    display: getComputedStyle(document.body).display,
    background: getComputedStyle(document.documentElement).backgroundColor,
    pending: document.documentElement.dataset.appearancePending,
  })), { display: "none", background: "rgba(0, 0, 0, 0)", pending: "true" });
  // Compare the entire iframe canvas with the parent exposed. This also catches
  // UA canvas fills and propagated body backgrounds which visibility misses.
  const pending = await page.screenshot();
  await page.locator("#studio").evaluate((element) => { element.style.visibility = "hidden"; });
  const parent = await page.screenshot();
  await page.locator("#studio").evaluate((element) => { element.style.visibility = ""; });
  assert.ok(pending.equals(parent), "waiting page painted a background or content");
}

async function successfulRead(browser, preference, mobile) {
  const context = await browser.newContext({ viewport: { width: mobile ? 390 : 1440, height: 844 }, colorScheme: "light" });
  const theme = { ...saved, preference };
  await context.addCookies([{ name: "image_studio_appearance_v1", value: encodeURIComponent(JSON.stringify(theme)), url: base, httpOnly: true, sameSite: "Lax" }]);
  await sampleFrames(context);
  const response = deferred(), scripts = deferred();
  await context.route("**/appearance", async (route) => { await response.promise; await route.continue(); });
  // The first theme read must start while unrelated application scripts still
  // block parsing, rather than waiting for DOMContentLoaded as before.
  await context.route("**/app.js?*", async (route) => { await scripts.promise; await route.continue(); });
  const request = context.waitForEvent("request", { predicate: (request) => request.url().endsWith("/appearance"), timeout: 3000 });
  try {
    const { page, frame } = await open(context);
    await request;
    await assertBlank(page, frame);
    response.resolve();
    await frame.waitForFunction(() => window.ImageStudioAppearance && document.documentElement.dataset.appearanceReady === "true");
    assert.equal(await frame.evaluate(() => document.readyState), "loading", "theme initialization waited for unrelated scripts");
    assert.deepEqual(await frame.evaluate(() => ImageStudioAppearance.get()), theme);
    await frame.waitForFunction(() => window.__appearanceFrames.some((frame) => frame.display !== "none"));
    const paints = await frame.evaluate(() => window.__appearanceFrames);
    for (const paint of paints.filter((paint) => paint.display !== "none")) {
      assert.equal(paint.pending, false);
      assert.equal(paint.theme, preference);
      assert.equal(paint.accent, "#db5778");
    }
    assert.equal(paints.find((paint) => paint.display !== "none").transitions, 0, "first visible frame animated from default colors");
    scripts.resolve();
    await page.waitForLoadState("load");
    await frame.locator('#modelChoice:not(:disabled)').waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[name="appearanceMode"]').first().waitFor({ state: "attached" });
    assert.equal(await frame.locator(`[name="appearanceMode"][value="${preference}"]`).isChecked(), true, "early read was lost when controls mounted");
    // Ordinary updates must keep the existing transitions and draft behavior.
    await frame.evaluate(() => ImageStudioAppearance.set({ accentHue: 210 }));
    assert.equal(await frame.evaluate(() => ImageStudioAppearance.isDirty()), true);
    assert.notEqual(await frame.locator(".nav-icon").first().evaluate((element) => getComputedStyle(element).transitionDuration), "0s");
    await page.reload();
    const reopened = await page.locator("#studio").elementHandle().then((element) => element.contentFrame());
    await reopened.waitForFunction(() => window.ImageStudioAppearance && document.documentElement.dataset.appearanceReady === "true");
    assert.deepEqual(await reopened.evaluate(() => ImageStudioAppearance.get()), theme, "reload restored an unsaved draft");
    console.log(`${preference} ${mobile ? "mobile" : "desktop"}: transparent wait, early read, first visible theme and reload passed`);
  } finally {
    response.resolve(); scripts.resolve();
    await context.close();
  }
}

async function fallback(browser, failure) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "light" });
  const response = deferred();
  if (failure === "script") {
    await context.route("**/appearance.js?*", (route) => route.abort());
  } else {
    await context.route("**/appearance", async (route) => {
      if (failure === "delayed") await response.promise;
      await route.fulfill({ status: failure === "error" ? 503 : 200, contentType: "application/json", body: JSON.stringify(failure === "error" ? { message: "unavailable" } : saved) });
    });
  }
  try {
    const { page, frame } = await open(context);
    await assertBlank(page, frame);
    await frame.waitForFunction(() => window.__imageStudioAppearanceGate.expired, null, { timeout: 6000 });
    assert.notEqual(await frame.locator("body").evaluate((element) => getComputedStyle(element).display), "none", "timeout left the application hidden");
    assert.ok(await frame.evaluate(() => performance.now()) >= 3900, "failure revealed the page before its deadline");
    if (failure !== "script") {
      const baseline = await frame.evaluate(() => ImageStudioAppearance.get());
      const returned = failure === "delayed" ? page.waitForResponse((response) => response.url().endsWith("/appearance")) : null;
      response.resolve();
      if (returned) await returned;
      await frame.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.deepEqual(await frame.evaluate(() => ImageStudioAppearance.get()), baseline, "late theme changed the visible fallback");
    }
    console.log(`${failure}: bounded transparent wait and safe fallback passed`);
  } finally {
    response.resolve();
    await context.close();
  }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSERS || "chromium,webkit").split(",")) {
    const browser = await playwright[engine].launch({ headless: true });
    try {
      await successfulRead(browser, "dark", true);
      await successfulRead(browser, "light", false);
      for (const failure of ["delayed", "error", "script"]) await fallback(browser, failure);
      console.log(`${engine}: appearance display gate passed`);
    } finally { await browser.close(); }
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
