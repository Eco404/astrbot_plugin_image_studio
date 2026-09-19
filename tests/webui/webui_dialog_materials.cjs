/* Run only against the isolated WebUI harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-dialog-materials-"));

async function settle(frame) {
  // Theme changes also update offscreen settings controls. WebKit may retain
  // their old transition promises; this check only needs the visible dialogs.
  await frame.waitForFunction(() => ["#studioModal", ".studio-modal-scrim", "#detailDrawer", ".scrim"].every(selector => {
    const element = document.querySelector(selector);
    return !element || element.getAnimations().every(animation => animation.playState !== "running" && animation.playState !== "pending");
  }), null, { timeout: 5000 });
  await frame.evaluate(async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function materials(frame) {
  return frame.evaluate(() => {
    const surface = selector => {
      const element = document.querySelector(selector), css = getComputedStyle(element);
      const context = document.createElement("canvas").getContext("2d");
      context.fillStyle = css.backgroundColor; context.fillRect(0, 0, 1, 1);
      return { color: [...context.getImageData(0, 0, 1, 1).data], filter: css.backdropFilter || css.webkitBackdropFilter };
    };
    const reference = surface("#detailDrawer"), footer = surface(".detail-footer");
    const panels = ["#studioModal", "#confirmDialog", "#parameterDialog", ".image-preview__panel", ".appearance-reset-confirmation"];
    const footers = ["#studioModalFooter", ".image-preview__actions", ".parameter-dialog__actions"];
    const modal = document.getElementById("studioModal"), previousClass = modal.className;
    const variants = ["", "is-external-editor", "is-import-editor", "is-merge-picker"].map(variant => {
      modal.className = `studio-modal glass ${variant}`;
      return { variant, surface: surface("#studioModal"), footer: surface("#studioModalFooter") };
    });
    modal.className = previousClass;
    return { reference, footer, confirmationActions: surface(".confirm-dialog__actions"), scrim: surface(".scrim"), scrims: [".studio-modal-scrim", ".image-preview__backdrop"].map(surface), panels: panels.map(selector => ({ selector, ...surface(selector) })), footers: footers.map(selector => ({ selector, ...surface(selector) })), variants };
  });
}

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(base);
    const frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.waitForURL(/\/ui\//);
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.evaluate(() => window.ImageStudioAppearance.ready);
    for (const preference of ["light", "dark"]) {
      for (const glassOpacity of [.2, .68, .9, 1]) {
        await frame.evaluate(patch => window.ImageStudioAppearance.set(patch), { preference, glassOpacity });
        const state = await materials(frame);
        const context = `${name} ${width} ${preference} ${glassOpacity}`;
        assert.ok(Math.abs(state.reference.color[3] / 255 - glassOpacity) < .005, context);
        for (const panel of state.panels) {
          assert.deepEqual(panel.color, state.reference.color, `${context}: ${panel.selector} follows drawer material`);
          assert.equal(panel.filter, state.reference.filter, `${context}: ${panel.selector} matches drawer blur`);
        }
        assert.ok(state.reference.filter.includes("blur(24px)") && state.reference.filter.includes("saturate(1.15)"), context);
        assert.ok(Math.abs(state.scrim.color[3] / 255 - .22) < .005, context);
        assert.equal(state.scrim.filter, "blur(3px)", context);
        for (const scrim of state.scrims) assert.deepEqual(scrim, state.scrim, `${context}: shared dialog scrim`);
        for (const footer of state.footers) {
          assert.deepEqual(footer.color, state.footer.color, `${context}: ${footer.selector} adds only footer tint`);
          assert.equal(footer.filter, "none", `${context}: ${footer.selector} does not stack another blur layer`);
        }
        assert.deepEqual(state.confirmationActions, { color: [0, 0, 0, 0], filter: "none" }, `${context}: compact confirmation uses its enclosing glass without an extra rectangular tint`);
        for (const variant of state.variants) {
          assert.deepEqual(variant.surface, state.reference, `${context}: ${variant.variant || "generic"}`);
          assert.deepEqual(variant.footer, state.footer, `${context}: ${variant.variant || "generic"} footer`);
        }
      }
    }
    console.log(`Checked ${name} ${width}: dialog panel and scrim styles`);
    // Exercise the reported delete dialog and external editor through real UI.
    await frame.evaluate(() => window.ImageStudioAppearance.discard());
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card .gallery-selection").first().click();
    await frame.locator("#deleteButton").click();
    await frame.locator("#confirmDialog:not(.is-hidden)").waitFor();
    await settle(frame);
    await page.screenshot({ path: path.join(output, `${name}-${width}-confirm.png`) });
    await frame.locator("#confirmCancel").click();
    await frame.locator("#confirmDialog.is-hidden").waitFor({ state: "attached" });
    await frame.locator("#cancelSelectionButton").click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator("#detailDelete").click();
    await frame.locator("#studioModalTitle").filter({ hasText: "删除图片" }).waitFor();
    await frame.evaluate(() => window.ImageStudioAppearance.set({ preference: "light", glassOpacity: .4 }));
    await settle(frame);
    assert.deepEqual((await materials(frame)).panels[0].color, (await materials(frame)).reference.color);
    await page.screenshot({ path: path.join(output, `${name}-${width}-delete.png`) });
    console.log(`Checked ${name} ${width}: delete dialog`);
    await frame.locator("#studioModalClose").click(); await settle(frame);
    await frame.locator("#closeDrawer").click(); await settle(frame);
    await frame.evaluate(() => window.ImageStudioAppearance.discard());
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#addExternalSource").click();
    await frame.locator("#studioModal.is-external-editor").waitFor();
    await frame.evaluate(() => window.ImageStudioAppearance.set({ preference: "dark", glassOpacity: .4 }));
    await settle(frame);
    await page.screenshot({ path: path.join(output, `${name}-${width}-external.png`) });
    await frame.locator("#studioModalClose").click(); await settle(frame);
    await frame.evaluate(() => window.ImageStudioAppearance.discard());
    assert.deepEqual(errors, []);
    console.log(`PASS ${name} ${width}: all dialog variants, themes, opacity, delete and external editor`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, launcher] of [["chromium", chromium], ["webkit", webkit]]) {
    if (process.env.STUDIO_BROWSER && process.env.STUDIO_BROWSER !== name) continue;
    const browser = await launcher.launch({ headless: true });
    try { for (const width of [1440, 390]) await verify(browser, name, width); }
    finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
