/* Isolated WebUI geometry checks; never confirms imports or saves settings. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-mobile-header-"));

async function frames(inner) {
  await inner.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function settle(inner) {
  await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))));
}

async function scroll(inner, y) {
  await inner.evaluate((value) => { document.documentElement.style.scrollBehavior = "auto"; window.scrollTo(0, value); }, y);
  await frames(inner);
}

async function dimensions(inner, name) {
  const value = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }));
  assert.ok(value.scrollWidth <= value.width + 1, `${name}: page overflow ${JSON.stringify(value)}`);
}

async function capture(page, inner, name) {
  await settle(inner); await dimensions(inner, name);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function headerState(inner) {
  return await inner.evaluate(() => {
    const sidebar = document.querySelector(".sidebar");
    const title = document.querySelector(".topbar");
    const style = getComputedStyle(sidebar); const rect = sidebar.getBoundingClientRect();
    return {
      scroll: window.scrollY, top: rect.top, height: rect.height,
      opacity: Number(style.opacity), offset: parseFloat(style.getPropertyValue("--brand-header-offset")) || 0,
      blur: parseFloat(style.getPropertyValue("--brand-header-blur")) || 0,
      variableOpacity: parseFloat(style.getPropertyValue("--brand-header-opacity")) || 0,
      sidebarZ: Number(style.zIndex), titleZ: Number(getComputedStyle(title).zIndex),
      titleTop: title.getBoundingClientRect().top,
      anchorTop: document.getElementById("pageHeaderAnchor").getBoundingClientRect().top,
    };
  });
}

async function openView(frame, inner, view) {
  await scroll(inner, 0);
  await frame.locator(`.nav-item[data-view="${view}"]`).click();
  if (view === "gallery") await frame.locator(".gallery-card").first().waitFor();
  if (view === "settings") await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  await inner.locator(`#${view}View`).evaluate((element) => { element.style.minHeight = "2000px"; });
  await scroll(inner, 0); await settle(inner);
}

async function checkRoundIcon(frame, selector, name) {
  const button = frame.locator(selector);
  const value = await button.evaluate((element) => {
    const rect = element.getBoundingClientRect(); const svg = element.querySelector("svg").getBoundingClientRect();
    return { width: rect.width, height: rect.height, radius: getComputedStyle(element).borderRadius, dx: svg.x + svg.width / 2 - rect.x - rect.width / 2, dy: svg.y + svg.height / 2 - rect.y - rect.height / 2 };
  });
  assert.ok(Math.abs(value.width - 44) < 1 && Math.abs(value.height - 44) < 1, `${name}: button must be 44px square ${JSON.stringify(value)}`);
  assert.ok(value.radius.includes("50%") || parseFloat(value.radius) >= 22, `${name}: button must be round`);
  assert.ok(Math.abs(value.dx) < 1 && Math.abs(value.dy) < 1, `${name}: icon off-center ${JSON.stringify(value)}`);
}

async function checkHeader(page, frame, inner, view, test) {
  await openView(frame, inner, view);
  const navBackground = await inner.locator(`.nav-item[data-view="${view}"]`).evaluate((button) => {
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1; const context = canvas.getContext("2d");
    const alpha = (element) => { context.clearRect(0, 0, 1, 1); context.fillStyle = getComputedStyle(element).backgroundColor; context.fillRect(0, 0, 1, 1); return context.getImageData(0, 0, 1, 1).data[3]; };
    return { outer: alpha(button), inner: alpha(button.querySelector(".nav-icon")) };
  });
  assert.ok(navBackground.inner > 0, `${test.name}/${view}: selected icon must retain its inner background`);
  assert.ok(test.width <= 900 ? navBackground.outer === 0 : navBackground.outer > 0, `${test.name}/${view}: unexpected navigation background ${JSON.stringify(navBackground)}`);
  if (test.width <= 540 && view === "generate") await checkRoundIcon(frame, "#pasteParametersButton", `${test.name}-paste`);
  if (test.width <= 540 && view === "gallery") await checkRoundIcon(frame, "#galleryRefresh", `${test.name}-refresh`);
  const initial = await headerState(inner);
  if (test.width > 900) {
    await scroll(inner, 220); const after = await headerState(inner);
    assert.equal(after.opacity, 1); assert.equal(after.offset, 0); assert.equal(after.blur, 0);
    await capture(page, inner, `${test.name}-${view}-desktop`); return;
  }
  assert.ok(Math.abs(initial.opacity - 1) < .02, `${test.name}/${view}: brand must start visible`);
  const distance = Math.max(1, initial.anchorTop - 8);
  await scroll(inner, distance / 2); const midpoint = await headerState(inner);
  assert.ok(midpoint.opacity > .1 && midpoint.opacity < .9, `${test.name}/${view}: missing intermediate fade ${JSON.stringify(midpoint)}`);
  assert.ok(Math.abs(midpoint.offset) < .01 && Math.abs(midpoint.top - initial.top) < 1, `${test.name}/${view}: underlying brand header must stay stationary`);
  assert.ok(midpoint.titleZ > midpoint.sidebarZ, `${test.name}/${view}: title must layer above the brand header`);
  assert.ok(midpoint.blur > 0 || test.reduced, `${test.name}/${view}: missing intermediate blur`);
  await capture(page, inner, `${test.name}-${view}-midpoint`);
  await page.waitForTimeout(180);
  const paused = await headerState(inner);
  assert.ok(Math.abs(paused.opacity - midpoint.opacity) < .01 && Math.abs(paused.top - midpoint.top) < .5, `${test.name}/${view}: scroll pause must not finish animation`);
  await scroll(inner, distance + 30); const replaced = await headerState(inner);
  assert.ok(replaced.opacity < .02, `${test.name}/${view}: brand should disappear after replacement`);
  assert.ok(Math.abs(replaced.offset) < .01 && Math.abs(replaced.top - initial.top) < 1, `${test.name}/${view}: fully covered brand must retain its position`);
  assert.ok(Math.abs(replaced.titleTop - 8) < 1.5, `${test.name}/${view}: title must pin at 8px ${JSON.stringify(replaced)}`);
  await capture(page, inner, `${test.name}-${view}-replaced`);
  await scroll(inner, distance / 2); const backwards = await headerState(inner);
  assert.ok(Math.abs(backwards.opacity - midpoint.opacity) < .02, `${test.name}/${view}: reverse scroll must restore midpoint`);
  await scroll(inner, 0); const restored = await headerState(inner);
  assert.ok(Math.abs(restored.opacity - 1) < .02 && Math.abs(restored.offset) < .5, `${test.name}/${view}: brand failed to restore`);
}

async function stageImport(inner) {
  await inner.evaluate(async () => {
    const canvas = document.createElement("canvas"); canvas.width = 36; canvas.height = 24;
    const context = canvas.getContext("2d"); context.fillStyle = "#75a89b"; context.fillRect(0, 0, 36, 24);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const files = new DataTransfer(); files.items.add(new File([blob], "mobile-header-preview.png", { type: "image/png" }));
    const input = document.getElementById("importFiles"); input.files = files.files; input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await inner.locator("#confirmImportButton:not(:disabled)").waitFor();
}

async function checkDetailFooter(page, frame, inner, test) {
  if (test.width > 540) return;
  await openView(frame, inner, "gallery");
  await frame.locator(".gallery-card .gallery-info").first().click();
  await frame.locator("[data-copy-field]").first().waitFor();
  await frame.locator("#detailUseReference:not(:disabled)").waitFor();
  for (const extra of [false, true]) {
    // Exercise both layouts without importing or modifying a stored record.
    await inner.locator("#detailWorkflowDownload").evaluate((button, visible) => { button.hidden = !visible; }, extra);
    await settle(inner);
    const layout = await inner.locator("#detailFooter").evaluate((footer) => {
      const items = Array.from(footer.querySelectorAll("#detailFavorite, #detailReproduce, .copy-format-picker, #detailCopy, #detailWorkflowDownload, #detailUseReference, #detailDelete")).filter((item) => item.getClientRects().length).map((item) => { const rect = item.getBoundingClientRect(); return { name: item.id || "format", x: rect.x, width: rect.width, center: rect.x + rect.width / 2, y: rect.y, height: rect.height }; });
      return { items, width: document.documentElement.clientWidth, footer: footer.getBoundingClientRect().toJSON() };
    });
    assert.equal(layout.items.length, extra ? 7 : 6, `${test.name}: footer actions unexpectedly hidden`);
    const gaps = layout.items.slice(1).map((item, index) => item.center - layout.items[index].center);
    assert.ok(Math.max(...gaps) - Math.min(...gaps) <= 1.5, `${test.name}: unequal footer center spacing ${JSON.stringify(layout)}`);
    assert.ok(layout.items.every((item) => item.x >= 0 && item.x + item.width <= layout.width + 1), `${test.name}: footer overflow`);
    assert.ok(Math.max(...layout.items.map((item) => item.y)) - Math.min(...layout.items.map((item) => item.y)) <= 1, `${test.name}: footer rows differ`);
    await capture(page, inner, `${test.name}-detail-${extra ? "seven" : "six"}-actions`);
  }
  await frame.locator("#closeDrawer").click();
}

async function checkSelectionHeader(page, frame, inner, test) {
  await openView(frame, inner, "gallery");
  await frame.locator(".gallery-selection").first().click();
  const values = await inner.evaluate(() => ({ selection: document.getElementById("selectionAnchor").getBoundingClientRect().top + window.scrollY, titleHeight: document.querySelector(".topbar").offsetHeight, sticky: innerWidth <= 900 ? 8 : 12 }));
  await scroll(inner, Math.max(0, values.selection - values.sticky - values.titleHeight / 2));
  const middle = await inner.locator(".topbar").evaluate((element) => ({ opacity: Number(getComputedStyle(element).opacity), replaced: element.classList.contains("is-selection-replaced") }));
  assert.ok(middle.replaced && middle.opacity > .01 && middle.opacity < .99, `${test.name}: selection bar no longer replaces title progressively ${JSON.stringify(middle)}`);
  const overlay = await inner.locator(".topbar").evaluate((element) => { const selection = document.getElementById("selectionBar"); return { top: element.getBoundingClientRect().top, offset: parseFloat(getComputedStyle(element).getPropertyValue("--selection-title-offset")) || 0, titleZ: Number(getComputedStyle(element).zIndex), selectionZ: Number(getComputedStyle(selection).zIndex) }; });
  assert.ok(Math.abs(overlay.top - values.sticky) < 1 && Math.abs(overlay.offset) < .01, `${test.name}: covered title must remain stationary ${JSON.stringify(overlay)}`);
  assert.ok(overlay.selectionZ > overlay.titleZ, `${test.name}: selection bar must layer above title`);
  await page.waitForTimeout(120);
  assert.ok(Math.abs(Number(await inner.locator(".topbar").evaluate((element) => getComputedStyle(element).opacity)) - middle.opacity) < .01, `${test.name}: paused selection handoff should remain at midpoint`);
  await scroll(inner, values.selection + 20);
  assert.ok(Number(await inner.locator(".topbar").evaluate((element) => getComputedStyle(element).opacity)) < .02);
  assert.ok(Math.abs((await inner.locator(".topbar").boundingBox()).y - values.sticky) < 1);
  await capture(page, inner, `${test.name}-selection-header`);
  await scroll(inner, Math.max(0, values.selection - values.sticky - values.titleHeight / 2));
  assert.ok(Math.abs(Number(await inner.locator(".topbar").evaluate((element) => getComputedStyle(element).opacity)) - middle.opacity) < .02, `${test.name}: reverse scroll should restore the title handoff midpoint`);
  await frame.locator("#cancelSelectionButton").click();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [320, 390, 540, 900, 1440]) {
      for (const theme of ["light", "dark"]) {
        const test = { width, theme, height: width <= 540 ? 844 : 1000, reduced: theme === "dark", name: `${width}-${theme}${theme === "dark" ? "-reduced" : ""}` };
        const page = await browser.newPage({ viewport: { width, height: test.height }, hasTouch: width <= 540, reducedMotion: test.reduced ? "reduce" : "no-preference" });
        page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(base);
        const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
        const inner = page.frames().find((item) => item.url().includes("/ui/"));
        await inner.evaluate(async (mode) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: mode }); }, theme);
        await frame.locator("#pageHeaderAnchor").waitFor({ state: "attached" });
        for (const view of ["generate", "gallery", "import", "settings"]) {
          if (view === "import") { await openView(frame, inner, view); await stageImport(inner); }
          await checkHeader(page, frame, inner, view, test);
        }
        await checkDetailFooter(page, frame, inner, test);
        await checkSelectionHeader(page, frame, inner, test);
        assert.deepEqual(errors, [], `${test.name}: browser errors`);
        await page.close(); console.log(`${test.name}: round controls, detail spacing, brand/title scroll, selection handoff passed`);
      }
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
