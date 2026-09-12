/* Ordinary dialog lifecycle and deep-scroll geometry, against isolated data. */
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-dialog-motion-"));

async function settle(frame) {
  await frame.evaluate(async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function geometry(frame) {
  return frame.evaluate(() => {
    const box = selector => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return { scroll: scrollY, sidebar: box(".sidebar"), topbar: box(".topbar") };
  });
}

async function sample(frame, trigger, root = "#studioModalRoot", panel = "#studioModal", value = null) {
  return frame.evaluate(async ({ trigger, root, panel, value }) => {
    const samples = [], start = performance.now();
    const control = document.querySelector(trigger);
    if (value === null) control.click();
    else {
      control.value = value;
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await new Promise(resolve => {
      function tick() {
        const layer = document.querySelector(root), content = document.querySelector(panel), style = getComputedStyle(content);
        const box = selector => {
          const rect = document.querySelector(selector).getBoundingClientRect();
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        };
        samples.push({ time: performance.now() - start, scroll: scrollY, sidebar: box(".sidebar"), topbar: box(".topbar"), opacity: Number(style.opacity), scale: style.transform === "none" ? 1 : new DOMMatrix(style.transform).a, hidden: layer.classList.contains("is-hidden"), closing: layer.classList.contains("is-closing"), locked: document.documentElement.classList.contains("modal-open"), bodyOverflow: getComputedStyle(document.body).overflowY });
        if (performance.now() - start < 300) requestAnimationFrame(tick); else resolve();
      }
      requestAnimationFrame(tick);
    });
    return samples;
  }, { trigger, root, panel, value });
}

function stable(before, samples, name) {
  for (const current of samples) {
    assert.ok(Math.abs(current.scroll - before.scroll) <= 1, `${name}: page scroll changed: ${JSON.stringify({ before, current })}`);
    for (const selector of ["sidebar", "topbar"]) for (const key of ["x", "y", "width", "height"]) {
      assert.ok(Math.abs(current[selector][key] - before[selector][key]) <= 1, `${name}: ${selector}.${key} changed: ${JSON.stringify({ before, current })}`);
    }
    assert.notEqual(current.bodyOverflow, "hidden", "body must not create another scroll container while a dialog is open");
  }
}

function animated(samples, closing, name) {
  assert.ok(samples.some(item => !item.hidden && item.opacity > .02 && item.opacity < .98 && item.scale > .984 && item.scale < .9999), `${name}: intermediate opacity and scale: ${JSON.stringify(samples)}`);
  if (closing) {
    assert.ok(samples.some(item => item.closing && item.locked && !item.hidden), `${name}: preserve layer and lock throughout exit`);
    assert.equal(samples.at(-1).hidden, true, `${name}: hide after exit`);
  } else {
    assert.equal(samples.at(-1).hidden, false);
    assert.equal(samples.at(-1).opacity, 1);
  }
}

async function verify(engine, name, width) {
  const browser = await engine.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 800 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], nativeDialogs = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => { nativeDialogs.push(dialog.type()); void dialog.dismiss(); });
  await page.addInitScript(() => {
    let factory;
    Object.defineProperty(window, "ImageStudioLibrary", { configurable: true, get: () => factory, set(value) {
      factory = (...args) => { const instance = value(...args); window.__dialogLibrary = instance; return instance; };
    } });
  });
  const source = { type: "directory", name: "动画测试图库", path: "/data/dialog-motion", enabled: false, recursive: false, permissions: { favorite: true, delete: false, download: true, reference: true } };
  await page.route("**/settings/get", async route => {
    const response = await route.fetch(), payload = await response.json();
    payload.webui.external_sources = { motion: source };
    for (const provider of payload.webui.providers) provider.discovered_models = provider.models.map(model => ({ id: model.id, name: model.name || model.id }));
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/external/status", route => route.fulfill({ json: { types: [{ id: "nai", name: "NAI 插件图库", path: "/data/plugin_data/astrbot_plugin_nai_image/image_history" }, { id: "directory", name: "自定义目录" }], sources: [{ id: "motion", ...source, status: "disabled", indexed_count: 0 }] } }));
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="settings"]').click();
    await frame.locator('[data-external-source="motion"]').waitFor();
    await settle(frame);
    await frame.evaluate(() => {
      const button = document.getElementById("addExternalSource");
      scrollTo(0, Math.max(600, button.getBoundingClientRect().top + scrollY - innerHeight * .45));
    });
    await settle(frame);
    const before = await geometry(frame);
    assert.ok(before.scroll >= 600, `${name}: exercise deep page scroll (${before.scroll})`);
    const entry = await sample(frame, "#addExternalSource");
    stable(before, entry, `${name} entry`); animated(entry, false, `${name} entry`);
    await page.screenshot({ path: path.join(output, `${name}-external-open.png`) });
    const exit = await sample(frame, "#studioModalClose");
    stable(before, exit, `${name} exit`); animated(exit, true, `${name} exit`);
    assert.equal(exit.at(-1).locked, false);

    // Real editor -> remove confirmation -> cancel -> restored editor chain.
    await frame.locator('[data-external-source="motion"]').click();
    await frame.locator("#externalEditorRemove").click();
    await frame.locator("#studioModalTitle").filter({ hasText: "移除图库" }).waitFor();
    await frame.locator("#studioModalFooter button").filter({ hasText: "取消" }).click();
    await frame.locator("#externalEditorName").waitFor();
    await settle(frame);
    assert.equal(await frame.locator("#externalEditorName").inputValue(), source.name);
    assert.equal(await frame.locator("#studioModalRoot").evaluate(element => element.classList.contains("is-hidden")), false);
    await frame.locator("#studioModalClose").click();
    await frame.locator("#studioModalRoot.is-hidden").waitFor({ state: "attached" });

    // Interruption during exit must settle the old promise but leave the new UI.
    await frame.evaluate(async () => {
      window.__firstDialog = window.__dialogLibrary.openModal("旧窗口", "<p>旧内容</p>", [{ label: "取消", action: () => false }]);
      document.getElementById("studioModalClose").click();
      await new Promise(resolve => setTimeout(resolve, 45));
      window.__secondDialog = window.__dialogLibrary.openModal("新窗口", "<p>新内容</p>", [{ label: "取消", action: () => false }]);
      window.__firstResult = await window.__firstDialog;
    });
    await settle(frame);
    assert.equal(await frame.evaluate(() => window.__firstResult), false);
    assert.equal(await frame.locator("#studioModalTitle").textContent(), "新窗口");
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true);
    await frame.locator("#studioModalClose").click();
    await frame.locator("#studioModalRoot.is-hidden").waitFor({ state: "attached" });

    // A retired async action must not re-enable or close the replacement modal.
    await frame.evaluate(() => {
      window.__dialogLibrary.openModal("旧操作", "", [{ label: "开始", id: "motionOld", action: () => new Promise(resolve => { window.__resolveOld = resolve; }) }]);
      document.getElementById("motionOld").click();
      window.__dialogLibrary.openModal("新操作", "", [{ label: "开始", id: "motionNew", action: () => new Promise(resolve => { window.__resolveNew = resolve; }) }]);
      document.getElementById("motionNew").click();
      window.__resolveOld(true);
    });
    await settle(frame);
    assert.equal(await frame.locator("#studioModalTitle").textContent(), "新操作");
    assert.equal(await frame.locator("#studioModal").getAttribute("aria-busy"), "true");
    assert.equal(await frame.locator("#motionNew").isDisabled(), true);
    await frame.evaluate(() => window.__resolveNew(true));
    await frame.locator("#studioModalRoot.is-hidden").waitFor({ state: "attached" });

    // Older app-owned dialogs follow the same lifecycle through real controls.
    const confirmEntry = await sample(frame, "#runDeepMaintenanceButton", "#confirmDialog", "#confirmDialog");
    animated(confirmEntry, false, `${name} confirmation entry`);
    const confirmExit = await sample(frame, "#confirmCancel", "#confirmDialog", "#confirmDialog");
    animated(confirmExit, true, `${name} confirmation exit`);
    assert.equal(confirmExit.at(-1).locked, false);
    await frame.locator('[data-model-tab="tool"]').click();
    await settle(frame);
    const parameterEntry = await sample(frame, "[data-edit-tool-parameter]", "#parameterDialog", "#parameterDialog");
    animated(parameterEntry, false, `${name} parameter entry`);
    const parameterExit = await sample(frame, "#parameterDialogCancel", "#parameterDialog", "#parameterDialog");
    animated(parameterExit, true, `${name} parameter exit`);
    assert.equal(parameterExit.at(-1).locked, false);

    // Manual model IDs use the ordinary dialog, with no native prompt fallback.
    await frame.locator('[data-model-tab="model"]').click();
    const modelId = frame.locator('select[data-model-field="id"]');
    const initialModelId = await modelId.inputValue();
    await modelId.evaluate(select => { select.value = "__manual__"; select.dispatchEvent(new Event("input", { bubbles: true })); });
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), false, "input alone must not launch the manual-ID dialog");
    const manualEntry = await sample(frame, 'select[data-model-field="id"]', "#studioModalRoot", "#studioModal", "__manual__");
    animated(manualEntry, false, `${name} manual model entry`);
    assert.equal(await frame.locator("#manualModelId").inputValue(), initialModelId);
    assert.equal(await modelId.inputValue(), initialModelId);
    const manualCancel = await sample(frame, "#studioModalFooter button:first-child");
    animated(manualCancel, true, `${name} manual model cancel`);
    assert.equal(await modelId.inputValue(), initialModelId);
    await modelId.selectOption("__manual__", { force: true });
    await frame.locator("#manualModelId").fill("   ");
    await frame.locator("#studioModalFooter button").filter({ hasText: "确认" }).click();
    await frame.locator("#studioModalError").filter({ hasText: "有效的模型 ID" }).waitFor();
    assert.equal(await modelId.inputValue(), initialModelId, "invalid input must leave the model untouched");
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true);
    await frame.locator("#manualModelId").fill(" motion-new-model ");
    const manualApply = await sample(frame, "#studioModalFooter button:last-child");
    animated(manualApply, true, `${name} manual model apply`);
    assert.equal(await modelId.inputValue(), "motion-new-model");
    assert.equal(await modelId.locator('option[value="motion-new-model"]').count(), 1);
    assert.deepEqual(nativeDialogs, []);

    // Closing a deletion picker layered over detail retains the parent and lock.
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#discardSettingsButton").click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await frame.locator("#detailDelete:not(:disabled)").waitFor();
    await frame.locator("#detailDelete").click();
    await frame.locator("#studioModalRoot:not(.is-hidden)").waitFor();
    await settle(frame);
    const nestedExit = await sample(frame, "#studioModalClose");
    animated(nestedExit, true, `${name} nested exit`);
    assert.equal(nestedExit.at(-1).locked, true);
    assert.equal(await frame.locator("#detailDrawer").getAttribute("aria-hidden"), "false");
    if (width >= 600) {
      const previewEntry = await sample(frame, "[data-detail-image]", "#imagePreview", ".image-preview__panel");
      animated(previewEntry, false, `${name} preview entry`);
      const previewExit = await sample(frame, "#closeImagePreview", "#imagePreview", ".image-preview__panel");
      animated(previewExit, true, `${name} preview exit`);
      assert.equal(previewExit.at(-1).locked, true);
      assert.equal(await frame.locator("#previewImage").getAttribute("src"), null);
    }
    await frame.locator("#closeDrawer").click();
    await settle(frame);

    // Reduced motion closes synchronously and never leaves a stale page lock.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await frame.locator('[data-view="settings"]').click();
    await frame.evaluate(() => document.getElementById("addExternalSource").click());
    assert.equal(await frame.locator("#studioModalRoot").evaluate(element => element.getAnimations({ subtree: true }).filter(animation => animation.effect.getTiming().duration > 1).length), 0);
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true);
    await frame.evaluate(() => document.getElementById("studioModalClose").click());
    assert.equal(await frame.locator("#studioModalRoot").evaluate(element => element.classList.contains("is-hidden")), true);
    assert.equal(await frame.evaluate(() => document.documentElement.classList.contains("modal-open")), false);
    assert.deepEqual(errors, []);
    console.log(`${name}: dialog entry/exit, deep-scroll geometry, chained/replaced dialogs, pending actions, parent lock and reduced motion passed`);
  } finally { await page.close(); await browser.close(); }
}

(async () => {
  await verify(chromium, "chromium-desktop", 1440);
  await verify(webkit, "webkit-mobile", 390);
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
