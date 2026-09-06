/* Surface-only UI checks; never saves plugin config, generates, imports or deletes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const segmentsOnly = process.env.STUDIO_SURFACE_SEGMENTS_ONLY === "1";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-surfaces-"));

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function appearance(frame, value) {
  await frame.waitForFunction(() => document.documentElement.dataset.appearanceReady === "true");
  await frame.evaluate(async (settings) => {
    await window.ImageStudioAppearance.ready;
    window.ImageStudioAppearance.set(settings);
    await window.ImageStudioAppearance.saved();
  }, value);
  await settle(frame);
}

async function openView(frame, view) {
  await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await frame.locator(`[data-view="${view}"]`).click();
  if (view === "gallery") await frame.locator(".gallery-card").first().waitFor();
  if (view === "settings") await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
  await settle(frame);
}

async function capture(page, frame, name) {
  await settle(frame);
  const width = await frame.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(width.scroll <= width.client + 1, `${name}: overflow ${JSON.stringify(width)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function navigation(frame, name) {
  const styles = await frame.locator(".nav-item, .nav-icon").evaluateAll((elements) => elements.map((element) => ({ name: element.className, shadow: getComputedStyle(element).boxShadow })));
  assert.ok(styles.length >= 8);
  assert.deepEqual(styles.filter((item) => item.shadow !== "none"), [], `${name}: navigation has a framed/shadowed layer`);
}

async function segmented(frame, selector, name) {
  await settle(frame);
  const groups = await frame.locator(selector).evaluateAll((elements) => elements.map((group) => {
    const items = Array.from(group.querySelectorAll(".segment, .model-tab, label > span")).map((element) => {
      const style = getComputedStyle(element);
      return {
        active: element.classList.contains("is-active") || !!element.previousElementSibling?.checked,
        shadow: style.boxShadow, background: style.backgroundColor, height: style.minHeight, radius: style.borderRadius,
      };
    });
    return { shadow: getComputedStyle(group).boxShadow, radius: getComputedStyle(group).borderRadius, items };
  }));
  assert.ok(groups.length > 0, `${name}: missing segmented control`);
  for (const group of groups) {
    assert.equal(group.shadow, "none", `${name}: grouped track must not introduce another frame`);
    assert.equal(group.radius, "9px");
    assert.equal(group.items.filter((item) => item.active).length, 1, `${name}: exactly one selection`);
    for (const item of group.items) {
      assert.equal(item.height, "34px"); assert.equal(item.radius, "7px");
      if (item.active) assert.match(item.shadow, /inset/, `${name}: selected segment has no frame`);
      else { assert.equal(item.shadow, "none", `${name}: inactive segment has a frame`); assert.equal(item.background, "rgba(0, 0, 0, 0)"); }
    }
  }
  return groups[0].items.find((item) => item.active).shadow;
}

async function ordinaryActions(frame, name) {
  const buttons = await frame.locator(".quiet-button, .primary-button, .danger-button, .studio-icon-button, .icon-button").evaluateAll((elements) => elements.filter((element) => element.getClientRects().length && !element.matches(".parameter-copy")).map((element) => ({ name: element.id || element.className, shadow: getComputedStyle(element).boxShadow })));
  assert.ok(buttons.length > 0);
  assert.deepEqual(buttons.filter((item) => item.shadow === "none"), [], `${name}: ordinary command lost its shadow`);
}

async function fieldCopy(frame, name, width) {
  const button = frame.locator(".parameter-copy").first();
  await button.waitFor();
  for (const hover of [false, true]) {
    if (hover) await button.hover();
    await settle(frame);
    const style = await button.evaluate((element) => {
      const computed = getComputedStyle(element); const bounds = element.getBoundingClientRect(); const icon = element.querySelector("svg").getBoundingClientRect();
      return { background: computed.backgroundColor, border: computed.borderWidth, shadow: computed.boxShadow, width: bounds.width, height: bounds.height, dx: icon.x + icon.width / 2 - bounds.x - bounds.width / 2, dy: icon.y + icon.height / 2 - bounds.y - bounds.height / 2 };
    });
    assert.equal(style.background, "rgba(0, 0, 0, 0)", `${name}: copy field hover background`);
    assert.equal(style.border, "0px"); assert.equal(style.shadow, "none");
    assert.equal(style.width, width <= 540 ? 44 : 28); assert.equal(style.height, style.width);
    assert.ok(Math.abs(style.dx) < 1 && Math.abs(style.dy) < 1, `${name}: copy field icon alignment`);
  }
  await button.focus(); await button.press("Tab"); await button.focus();
  assert.equal(await button.evaluate((element) => getComputedStyle(element).outlineStyle), "solid");
  assert.notEqual(await frame.locator("#detailCopy").evaluate((element) => getComputedStyle(element).boxShadow), "none", `${name}: full-parameter copy must retain its button surface`);
  await button.evaluate((element) => element.blur());
}

async function surfaces(frame, selectors, name) {
  for (const opacity of [0.2, 0.68, 1]) {
    await appearance(frame, { glassOpacity: opacity });
    const result = await frame.evaluate((selectors) => {
      const sample = document.createElement("div"); sample.style.backgroundColor = "var(--glass)"; document.body.appendChild(sample);
      const glass = getComputedStyle(sample).backgroundColor;
      sample.remove();
      const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1; const context = canvas.getContext("2d");
      const alpha = (color) => { context.clearRect(0, 0, 1, 1); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return context.getImageData(0, 0, 1, 1).data[3] / 255; };
      return { glass, alpha: alpha(glass), surfaces: selectors.map((selector) => {
        const element = document.querySelector(selector); if (!element) return { selector, missing: true };
        const ownStyle = getComputedStyle(element);
        const usesParent = element.classList.contains("detail-footer");
        const style = usesParent ? getComputedStyle(element.closest(".detail-drawer")) : ownStyle;
        return { selector, usesParent, ownAlpha: alpha(ownStyle.backgroundColor), ownBlur: ownStyle.backdropFilter || ownStyle.webkitBackdropFilter, background: style.backgroundColor, alpha: alpha(style.backgroundColor), blur: style.backdropFilter || style.webkitBackdropFilter, visible: element.getClientRects().length > 0 };
      }) };
    }, selectors);
    assert.ok(Math.abs(result.alpha - opacity) < .012, `${name}: opacity outside requested range ${JSON.stringify(result)}`);
    for (const surface of result.surfaces) {
      assert.ok(!surface.missing && surface.visible, `${name}: missing surface ${surface.selector}`);
      assert.equal(surface.background, result.glass, `${name}: ${surface.selector} does not share --glass`);
      assert.ok(Math.abs(surface.alpha - opacity) < .012, `${name}: ${surface.selector} opacity`);
      if (surface.usesParent) {
        const effectiveAlpha = 1 - (1 - surface.alpha) * (1 - surface.ownAlpha);
        assert.ok(Math.abs(effectiveAlpha - Math.min(1, opacity + .06)) < .012, `${name}: footer tint must add only six opacity points`);
        assert.equal(surface.ownBlur, "none", `${name}: footer adds a second backdrop layer`);
      }
      assert.match(surface.blur, surface.usesParent ? /blur\(24px\)/ : /blur\(22px\)/, `${name}: ${surface.selector} blur missing`);
      assert.match(surface.blur, /saturate\(1\.15\)/, `${name}: ${surface.selector} saturation missing`);
    }
  }
  await appearance(frame, { glassOpacity: 0.68 });
}

async function controls(page, frame, test, name) {
  await appearance(frame, { preference: test.theme, glassOpacity: 0.68 });
  await navigation(frame, name);
  const generationShadow = await segmented(frame, ".mode-tabs", `${name}-generation`);
  await frame.locator('[data-mode="img2img"]').click();
  await segmented(frame, ".mode-tabs", `${name}-generation-changed`);
  await ordinaryActions(frame, `${name}-generation`);
  await capture(page, frame, `${name}-generation`);
  await openView(frame, "settings");
  await navigation(frame, name);
  assert.equal(await segmented(frame, ".default-tabs", `${name}-defaults`), generationShadow);
  assert.equal(await segmented(frame, ".appearance-modes", `${name}-appearance`), generationShadow);
  await frame.locator('[data-default-scope="tool"]').click();
  await segmented(frame, ".default-tabs", `${name}-defaults-changed`);
  await frame.locator('[data-settings-provider="natural"]').click();
  await frame.locator('[data-model-tab="tool"]').click();
  await segmented(frame, "#modelForm > .model-tabs", `${name}-model-tool`);
  await frame.locator('[data-model-tab="model"]').click();
  await segmented(frame, "#modelForm > .model-tabs", `${name}-model-config`);
  await frame.locator("#appearanceSettings").scrollIntoViewIfNeeded();
  await ordinaryActions(frame, `${name}-settings`);
  const target = frame.locator('.appearance-modes input[value="' + test.theme + '"]');
  await target.focus(); await target.press("Tab"); await target.focus();
  assert.equal(await target.locator("+ span").evaluate((element) => getComputedStyle(element).outlineStyle), "solid");
  await capture(page, frame, `${name}-settings`);
  if (segmentsOnly) return;
  await surfaces(frame, [".topbar", ".settings-savebar"], `${name}-settings`);
  await openView(frame, "import");
  await surfaces(frame, [".topbar", ".import-floatingbar"], `${name}-import`);
  await capture(page, frame, `${name}-import`);
  await openView(frame, "gallery");
  await surfaces(frame, [".topbar", ".gallery-floatingbar"], `${name}-gallery`);
  await frame.locator(".gallery-selection").first().click();
  await surfaces(frame, ["#selectionBar"], `${name}-selection`);
  await capture(page, frame, `${name}-gallery`);
  await frame.locator("#cancelSelectionButton").click();
  await frame.locator(".gallery-card .gallery-info").first().click();
  await frame.locator("#detailDelete:not(:disabled)").waitFor();
  const detailBlur = await frame.locator("#detailDrawer").evaluate((element) => {
    const style = getComputedStyle(element); return style.backdropFilter || style.webkitBackdropFilter;
  });
  assert.match(detailBlur, /blur\(24px\).*saturate\(1\.15\)/, `${name}: detail drawer must blur the underlying page in Safari too`);
  await fieldCopy(frame, name, test.width);
  await surfaces(frame, [".detail-footer"], `${name}-detail`);
  await capture(page, frame, `${name}-detail`);
  await frame.locator("#detailDelete").click();
  await frame.locator("#studioModalFooter").waitFor();
  await surfaces(frame, ["#studioModalFooter"], `${name}-modal`);
  await capture(page, frame, `${name}-modal`);
  await frame.locator("#studioModalClose").click();
  if (test.width > 540) {
    await frame.locator(".detail-image-frame:not([aria-busy]) .detail-image[src]").waitFor();
    await frame.locator(".detail-image").click();
    await frame.locator("#imagePreview:not(.is-hidden)").waitFor();
    await surfaces(frame, [".image-preview__actions"], `${name}-preview`);
    await capture(page, frame, `${name}-preview`);
    await frame.locator("#closeImagePreview").click();
  }
  await frame.locator("#closeDrawer").click();
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const test of [{ width: 1440, theme: "light" }, { width: 1100, theme: "dark" }, { width: 390, theme: "light" }, { width: 320, theme: "dark" }]) {
      const name = `${engine}-${test.width}-${test.theme}`;
      const context = await browser.newContext({ viewport: { width: test.width, height: test.width > 540 ? 1000 : 844 }, hasTouch: test.width <= 540, reducedMotion: test.theme === "dark" ? "reduce" : "no-preference" });
      const page = await context.newPage(); page.setDefaultTimeout(12000); const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(base);
      assert.equal(await page.locator("#studio").count(), 1, "expected isolated harness");
      const frame = page.frames().find((item) => item.url().includes("/ui/"));
      await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      await controls(page, frame, test, name);
      assert.deepEqual(errors, [], `${name}: browser errors`);
      await context.close();
      console.log(`${name}: unframed navigation, selected-only segment frames${segmentsOnly ? "" : ", shared translucent floating surfaces"} passed`);
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
