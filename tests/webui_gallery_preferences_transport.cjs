/* Browser-side transport failures and ordering, without backend/runtime data. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const source = path.resolve(__dirname, "../pages/image-studio/gallery-preferences.js");

async function create(page) {
  await page.setContent("<!doctype html><meta charset=utf-8><title>Opaque preference transport</title>");
  await page.evaluate(() => {
    Object.defineProperty(window, "localStorage", { configurable: true, get() { throw new DOMException("Opaque origin", "SecurityError"); } });
    window.__cookie = { sort: "latest_content", filters: { gallerySource: { mode: "values", values: ["command"] } } };
    window.__calls = []; window.__active = 0; window.__maxActive = 0; window.__blockCookie = false;
    const clone = value => JSON.parse(JSON.stringify(value));
    window.__client = {
      apiGet: async (route) => {
        if (route !== "gallery/preferences") throw new Error("Wrong API route");
        window.__calls.push("GET");
        if (window.__holdRead) { window.__holdRead = false; return new Promise(resolve => { window.__releaseRead = resolve; }); }
        return clone(window.__cookie);
      },
      apiPost: async (route, patch) => {
        if (route !== "gallery/preferences") throw new Error("Wrong API route");
        window.__calls.push("POST"); window.__active++; window.__maxActive = Math.max(window.__maxActive, window.__active);
        // Cookie responses may race: each request merges the snapshot it saw.
        const next = { sort: patch.sort ?? window.__cookie.sort, filters: { ...clone(window.__cookie.filters), ...clone(patch.filters || {}) } };
        await new Promise(resolve => setTimeout(resolve, 20));
        if (!window.__blockCookie) window.__cookie = next;
        window.__active--;
        return clone(next);
      },
    };
  });
  await page.addScriptTag({ path: source });
}

async function verify(browser, label) {
  const page = await browser.newPage();
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  try {
    await create(page);
    assert.deepEqual(await page.evaluate(() => window.__calls), [], "loading the module must not start bridge requests");
    await page.evaluate(() => window.ImageStudioGalleryPreferences.ready(window.__client));
    assert.equal(await page.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "latest_content");
    assert.deepEqual(await page.evaluate(() => window.ImageStudioGalleryPreferences.getFilter("gallerySource")), { mode: "values", values: ["command"] });
    await page.evaluate(() => Promise.all([
      window.ImageStudioGalleryPreferences.setFilter("galleryProvider", { mode: "values", values: ["", "nai"] }),
      window.ImageStudioGalleryPreferences.setSort("created"),
      window.ImageStudioGalleryPreferences.setFilter("gallerySource", { mode: "all" }),
    ]));
    assert.deepEqual(await page.evaluate(() => window.__calls), ["GET", "POST", "GET", "POST", "GET", "POST", "GET"], "each cookie save must finish verification before the next merge");
    assert.equal(await page.evaluate(() => window.__maxActive), 1);
    assert.deepEqual(await page.evaluate(() => window.__cookie), { sort: "created", filters: { gallerySource: { mode: "all" }, galleryProvider: { mode: "values", values: ["", "nai"] } } });
    const failure = await page.evaluate(async () => {
      window.__blockCookie = true;
      try { await window.ImageStudioGalleryPreferences.setSort("latest_content"); return "unexpected success"; }
      catch (error) { return error.message; }
    });
    assert.match(failure, /Cookie/);
    assert.equal(await page.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "created", "unverified writes must not become persisted state");
    await page.evaluate(async () => { window.__blockCookie = false; await window.ImageStudioGalleryPreferences.setSort("latest_content"); });
    assert.equal(await page.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "latest_content", "a failed save must not poison the write queue");
    const validation = await page.evaluate(async () => {
      const before = window.__calls.length;
      const statuses = await Promise.allSettled([
        window.ImageStudioGalleryPreferences.setFilter("galleryProvider", { mode: "values", values: [] }),
        window.ImageStudioGalleryPreferences.setFilter("unrelated", { mode: "all" }),
        window.ImageStudioGalleryPreferences.setSort("invalid"),
      ]);
      return { unchanged: before === window.__calls.length, statuses: statuses.map(result => result.status) };
    });
    assert.deepEqual(validation, { unchanged: true, statuses: ["rejected", "rejected", "rejected"] });

    // Recreate the module for a timed-out initial read. Late results must not
    // replace preferences successfully saved after startup continued.
    await page.reload(); await create(page);
    const timeout = await page.evaluate(async () => {
      window.__holdRead = true;
      const originalTimeout = window.setTimeout;
      window.setTimeout = (callback, delay, ...args) => originalTimeout(callback, delay === 4000 ? 20 : delay, ...args);
      try { await window.ImageStudioGalleryPreferences.ready(window.__client); return "unexpected success"; }
      catch (error) { return error.message; }
    });
    assert.match(timeout, /超时/);
    await page.evaluate(() => window.ImageStudioGalleryPreferences.setFilter("galleryEngine", { mode: "values", values: ["novelai"] }));
    await page.evaluate(async () => { window.__releaseRead({ sort: "created", filters: {} }); await Promise.resolve(); await Promise.resolve(); });
    assert.deepEqual(await page.evaluate(() => window.ImageStudioGalleryPreferences.getFilter("galleryEngine")), { mode: "values", values: ["novelai"] });
    assert.equal(await page.evaluate(() => window.ImageStudioGalleryPreferences.getSort()), "latest_content", "late initial data must not replace verified cookie state");
    assert.deepEqual(errors, []);
    console.log(`${label}: opaque-origin transport, serialized cookie patches, verification failure/retry, invalid inputs, startup timeout and late-response guard passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [label, type] of [["Chromium", chromium], ["WebKit", webkit]]) {
    const browser = await type.launch({ headless: true });
    try { await verify(browser, label); } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
