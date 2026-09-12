/* Settings layout and dynamic content, against isolated test data only. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-settings-layout-"));
const order = ["运行", "默认值", "历史", "图片资产", "外部图库", "存储健康", "主题与显示"];

async function settle(frame) {
  await frame.evaluate(async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function geometry(frame, columns) {
  const result = await frame.locator(".settings-layout").evaluate(grid => {
    const bounds = grid.getBoundingClientRect();
    return { height: bounds.height, width: bounds.width, gap: parseFloat(getComputedStyle(grid).columnGap), cards: [...grid.children].map(card => {
      const rect = card.getBoundingClientRect();
      return { title: card.querySelector("h2").textContent, x: rect.x - bounds.x, y: rect.y - bounds.y, width: rect.width, height: rect.height, margin: parseFloat(getComputedStyle(card).marginBottom), column: Number(card.style.gridColumn) };
    }) };
  });
  assert.deepEqual(result.cards.map(card => card.title), order);
  const ends = Array(columns).fill(0), width = (result.width - result.gap * (columns - 1)) / columns;
  for (const card of result.cards) {
    assert.ok(card.column >= 1 && card.column <= columns);
    assert.ok(Math.abs(card.width - width) < 1.1, `${card.title}: correct column width`);
    assert.ok(Math.abs(card.x - (card.column - 1) * (width + result.gap)) < 1.1);
    assert.ok(Math.abs(card.y - ends[card.column - 1]) < 1.1, `${card.title}: no gap or overlap`);
    ends[card.column - 1] = card.y + Math.ceil(card.height + card.margin - 0.001);
  }
  assert.ok(Math.abs(result.height - Math.max(...ends)) < 1.1, "container encloses both columns");
  if (columns === 2) assert.deepEqual(result.cards.slice(0, 2).map(card => card.column), [1, 2]);
  return result;
}

async function verify(browser, name) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, hasTouch: true });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor();
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    await settle(frame);
    await geometry(frame, 2);
    await page.screenshot({ path: path.join(output, `${name}-desktop.png`) });
    // Equal-size cards expose the required tie rule, not simple L/R pairing.
    await frame.locator(".settings-layout").evaluate(grid => [...grid.children].forEach(card => { card.style.height = "100px"; card.style.overflow = "hidden"; }));
    await settle(frame);
    assert.deepEqual((await geometry(frame, 2)).cards.map(card => card.column), [1, 2, 2, 1, 1, 2, 2]);
    await frame.locator(".settings-layout > section").first().evaluate(card => { card.style.height = "600px"; });
    await settle(frame);
    assert.deepEqual((await geometry(frame, 2)).cards.map(card => card.column), [1, 2, 2, 2, 2, 2, 2]);
    await frame.locator(".settings-layout").evaluate(grid => [...grid.children].forEach(card => { card.style.removeProperty("height"); card.style.removeProperty("overflow"); }));
    await settle(frame);
    await geometry(frame, 2);
    // Late content (scan entries/health errors) expands one card independently.
    await frame.locator("#externalSourcesPanel").evaluate(card => { const block = document.createElement("div"); block.id = "layout-test-content"; block.style.height = "420px"; card.append(block); });
    await settle(frame);
    await geometry(frame, 2);
    await frame.locator("#layout-test-content").evaluate(block => block.remove());
    for (const width of [1100, 900, 390, 1440]) {
      await page.setViewportSize({ width, height: width < 600 ? 844 : 1000 });
      await settle(frame);
      await geometry(frame, width <= 900 ? 1 : 2);
      await frame.locator('[data-view="generate"]').click();
      await frame.locator('[data-view="settings"]').click();
      await settle(frame);
      await geometry(frame, width <= 900 ? 1 : 2);
      if (width === 390) await page.screenshot({ path: path.join(output, `${name}-mobile.png`) });
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    await frame.locator("#externalSourcesPanel").evaluate(card => { card.style.paddingBottom = "180px"; });
    await settle(frame);
    await geometry(frame, 2);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const entrance = await frame.locator("#addExternalSource").evaluate(button => {
      button.click();
      const animation = document.getElementById("studioModal").getAnimations()[0];
      return { duration: animation.effect.getTiming().duration, easing: animation.effect.getTiming().easing, frames: animation.effect.getKeyframes() };
    });
    assert.equal(entrance.duration, 180); assert.equal(entrance.easing, "ease");
    assert.equal(entrance.frames[0].opacity, "0"); assert.ok(entrance.frames[0].transform.includes(".985"));
    await frame.locator("#studioModalClose").click();
    await frame.locator("#addExternalSource").click();
    await settle(frame);
    assert.equal(await frame.locator("#studioModal").isVisible(), true, "closing and reopening cannot hide the newer modal");
    await frame.locator("#studioModalClose").click();
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card").first().waitFor();
    await frame.locator("#galleryPageLabel").click();
    const material = await frame.locator("#galleryPagePicker").evaluate(picker => {
      const bar = document.querySelector(".gallery-floatingbar"), a = getComputedStyle(picker), b = getComputedStyle(bar);
      return { siblings: picker.parentElement === bar.parentElement, background: a.backgroundColor, expected: b.backgroundColor, blur: a.backdropFilter || a.webkitBackdropFilter, expectedBlur: b.backdropFilter || b.webkitBackdropFilter, animation: a.animationName };
    });
    assert.ok(material.siblings, "page picker must not be trapped inside another backdrop root");
    assert.equal(material.background, material.expected); assert.equal(material.blur, material.expectedBlur);
    assert.equal(material.animation, "studio-view-enter");
    await page.emulateMedia({ reducedMotion: "reduce" });
    const duration = await frame.locator("#galleryPagePicker").evaluate(picker => parseFloat(getComputedStyle(picker).animationDuration));
    assert.ok(duration <= .001, "reduced motion disables the entrance fade");
    assert.deepEqual(errors, [], "no runtime or ResizeObserver loop errors");
    console.log(`${name}: settings column flow, ties, late content, resizing, revisit and reduced motion passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await engine.launch({ headless: true });
    try { await verify(browser, name); } finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
