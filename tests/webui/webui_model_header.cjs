/* Uses temporary browser drafts only; never saves plugin settings. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-model-header-"));

(async () => {
  for (const engine of (process.env.STUDIO_BROWSERS || "chromium,webkit").split(",")) {
    const browser = await playwright[engine].launch({ headless: true });
    try {
      for (const width of [1440, 1100, 900, 720, 540, 390, 320]) {
        const context = await browser.newContext({ viewport: { width, height: 900 }, hasTouch: width <= 540 });
        try {
          const page = await context.newPage();
          const errors = [];
          page.on("pageerror", (error) => errors.push(error.message));
          await page.goto(base);
          const frame = page.frames().find((item) => item.url().includes("/ui/"));
          await frame.waitForFunction(() => document.documentElement.dataset.appearanceReady === "true");
          await frame.locator('[data-view="settings"]').click();
          await frame.locator("#addModelButton:not(:disabled)").waitFor();
          for (const preference of ["light", "dark"]) {
            await frame.evaluate((value) => window.ImageStudioAppearance.set({ preference: value }), preference);
            await frame.locator("#addModelButton").scrollIntoViewIfNeeded();
            const geometry = await frame.locator("#addModelButton").evaluate((button) => {
              const range = document.createRange(); range.selectNodeContents(button);
              const rects = [...range.getClientRects()];
              const bounds = button.getBoundingClientRect();
              const input = document.getElementById("newModelChoice").getBoundingClientRect();
              const header = button.closest(".section-heading").getBoundingClientRect();
              return { lines: new Set(rects.map((rect) => Math.round(rect.top))).size, fits: rects.every((rect) => rect.left >= bounds.left && rect.right <= bounds.right), gap: bounds.left - input.right, contained: bounds.right <= header.right + 1 && input.left >= header.left - 1, width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth };
            });
            assert.equal(geometry.lines, 1, `${engine} ${width} ${preference}: add model text wrapped`);
            assert.ok(geometry.fits && geometry.contained && geometry.gap >= 0, JSON.stringify(geometry));
            assert.ok(geometry.scroll <= geometry.width + 1, JSON.stringify(geometry));
            await frame.locator(".model-settings > .section-heading").screenshot({ path: path.join(output, `${engine}-${width}-${preference}.png`) });
          }
          const draftId = `header-draft-${engine}-${width}`;
          await frame.locator("#newModelChoice").fill(draftId);
          await frame.locator("#addModelButton").click();
          await frame.waitForFunction((value) => document.querySelector('#modelForm [data-model-field="id"]')?.value === value, draftId);
          assert.deepEqual(errors, []);
          console.log(`${engine} ${width}px: single line, layout, light/dark, add draft passed`);
        } finally { await context.close(); }
      }
    } finally { await browser.close(); }
  }
  console.log(`Model header screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
