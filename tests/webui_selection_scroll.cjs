/* Run against the isolated webui_harness.py; this test never deletes records. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL || "http://127.0.0.1:18765";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-selection-"));

async function scrollTo(frame, top) {
  await frame.evaluate(async (position) => {
    window.scrollTo({ top: position, behavior: "instant" });
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, top);
}

async function geometry(frame) {
  return frame.evaluate(() => {
    const header = document.querySelector(".topbar");
    const bar = document.getElementById("selectionBar");
    const title = getComputedStyle(header);
    const headerBounds = header.getBoundingClientRect();
    const barBounds = bar.getBoundingClientRect();
    const overlapTop = Math.max(headerBounds.top, barBounds.top);
    const overlapBottom = Math.min(headerBounds.bottom, barBounds.bottom);
    const overlapLeft = Math.max(headerBounds.left, barBounds.left);
    const overlapRight = Math.min(headerBounds.right, barBounds.right);
    const overlapOwner = overlapTop < overlapBottom && overlapLeft < overlapRight
      ? document.elementFromPoint((overlapLeft + overlapRight) / 2, (overlapTop + overlapBottom) / 2)
      : null;
    return {
      scroll: window.scrollY,
      opacity: Number(title.opacity),
      blur: title.filter,
      transform: title.transform,
      titleTop: header.getBoundingClientRect().top,
      titleInset: parseFloat(title.top),
      titleZIndex: Number(title.zIndex),
      barZIndex: Number(getComputedStyle(bar).zIndex),
      barCoversTitle: !!overlapOwner && (overlapOwner === bar || bar.contains(overlapOwner)),
      barTop: bar.getBoundingClientRect().top,
      inset: parseFloat(getComputedStyle(bar).top),
      barWidth: bar.getBoundingClientRect().width,
      barScrollWidth: bar.scrollWidth,
      pageWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      anchorTop: document.getElementById("selectionAnchor").getBoundingClientRect().top + window.scrollY,
      distance: header.offsetHeight + 10,
    };
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const test of [
      { width: 1440, height: 1000, theme: "light" },
      { width: 1100, height: 900, theme: "dark" },
      { width: 900, height: 1000, theme: "light" },
      { width: 390, height: 844, theme: "light" },
      { width: 360, height: 800, theme: "dark" },
      { width: 390, height: 844, theme: "dark", reducedMotion: "reduce" },
    ]) {
      const name = `${test.width}-${test.theme}${test.reducedMotion ? "-reduced" : ""}`;
      const page = await browser.newPage({ viewport: { width: test.width, height: test.height }, reducedMotion: test.reducedMotion || "no-preference" });
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(base);
      const frame = page.frames().find(item => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await frame.evaluate(theme => { document.documentElement.dataset.theme = theme; }, test.theme);
      await frame.locator('[data-view="gallery"]').click();
      await frame.locator(".gallery-selection input").first().check();
      await frame.evaluate(async () => {
        await Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {})));
      });
      await scrollTo(frame, 0);
      const initial = await geometry(frame);
      assert.equal(initial.opacity, 1);
      assert.ok(initial.barTop > initial.titleTop + initial.distance);
      assert.equal(await frame.locator(".gallery-floatingbar #selectionBar").count(), 0);
      await page.screenshot({ path: path.join(output, `${name}-initial.png`) });
      const midpointScroll = initial.anchorTop - initial.inset - initial.distance / 2;
      await scrollTo(frame, midpointScroll);
      const middle = await geometry(frame);
      assert.ok(middle.opacity > 0.4 && middle.opacity < 0.6, JSON.stringify(middle));
      assert.ok(Math.abs(middle.titleTop - initial.titleInset) < 1, "the outgoing title must remain fixed at its sticky inset");
      assert.equal(middle.transform, "none", "the title must not slide with the incoming selection bar");
      assert.ok(middle.barZIndex > middle.titleZIndex && middle.barCoversTitle, "the selection bar must visually cover the fixed title in their overlap");
      assert.ok(middle.blur.startsWith("blur("));
      assert.ok(Math.abs(parseFloat(middle.blur.slice(5)) - 4) < 0.2, "title blur should be proportional to the half-completed overlap");
      assert.ok(middle.barTop > initial.inset);
      await page.screenshot({ path: path.join(output, `${name}-middle.png`) });
      // Time passing without scrolling must not finish the handoff.
      await page.waitForTimeout(350);
      const paused = await geometry(frame);
      assert.equal(paused.opacity, middle.opacity);
      assert.equal(paused.transform, middle.transform);
      assert.equal(paused.blur, middle.blur);
      assert.equal(paused.titleTop, middle.titleTop);
      await scrollTo(frame, initial.anchorTop + 180);
      const pinned = await geometry(frame);
      assert.equal(pinned.opacity, 0);
      assert.ok(Math.abs(pinned.titleTop - middle.titleTop) < 1, "the covered title must stay fixed after the bar pins");
      assert.equal(pinned.transform, "none");
      assert.ok(pinned.barZIndex > pinned.titleZIndex && pinned.barCoversTitle);
      assert.ok(Math.abs(pinned.barTop - pinned.inset) < 1);
      assert.ok(pinned.pageScrollWidth <= pinned.pageWidth + 1);
      assert.ok(pinned.barScrollWidth <= pinned.barWidth + 1);
      await page.screenshot({ path: path.join(output, `${name}-pinned.png`) });
      await page.screenshot({ path: path.join(output, `${name}-full.png`), fullPage: true });
      await frame.locator("#selectAllButton").click();
      assert.ok(await frame.locator(".gallery-selection input:checked").count() >= 24);
      await scrollTo(frame, midpointScroll);
      const reversed = await geometry(frame);
      assert.equal(reversed.opacity, middle.opacity);
      assert.equal(reversed.titleTop, middle.titleTop);
      assert.equal(reversed.transform, "none");
      await scrollTo(frame, initial.anchorTop + 180);
      await frame.locator("#cancelSelectionButton").click();
      const cleared = await geometry(frame);
      assert.equal(cleared.opacity, 1);
      assert.equal(cleared.transform, "none");
      assert.equal(await frame.locator(".gallery-selection input:checked").count(), 0);
      await scrollTo(frame, 0);
      await frame.locator(".gallery-selection input").first().check();
      await scrollTo(frame, initial.anchorTop + 180);
      await frame.evaluate(() => document.querySelector('[data-view="generate"]').click());
      assert.equal((await geometry(frame)).opacity, 1);
      await frame.evaluate(() => document.querySelector('[data-view="gallery"]').click());
      await frame.locator(".gallery-card").first().waitFor();
      await scrollTo(frame, 0);
      assert.equal((await geometry(frame)).opacity, 1);
      assert.deepEqual(errors, []);
      console.log(`${name}: initial, proportional handoff, pause, reverse, pin, actions and reset passed`);
      await page.close();
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
