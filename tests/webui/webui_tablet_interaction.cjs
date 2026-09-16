/* Touch-tablet interaction coverage against the isolated WebUI harness only. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-tablet-"));

async function frames(frame, count = 3) {
  await frame.evaluate(remaining => new Promise(resolve => {
    const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick);
    requestAnimationFrame(tick);
  }), count);
}

async function touch(frame, type, dx = 0) {
  await frame.evaluate(({ type, dx }) => {
    const image = document.querySelector(".detail-image-frame");
    const box = image.getBoundingClientRect();
    const point = { identifier: 71, target: image, clientX: box.x + box.width * .7 + dx, clientY: box.y + box.height * .4 };
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: ["touchend", "touchcancel"].includes(type) ? [] : [point] },
      changedTouches: { value: [point] },
    });
    image.dispatchEvent(event);
  }, { type, dx });
}

async function selected(frame, index) {
  await frame.waitForFunction(index => {
    const image = document.querySelector(".detail-image-frame");
    return image?.querySelector("[data-detail-image]")?.dataset.detailImage === String(index) && !image.dataset.detailSwipeState;
  }, index);
}

async function viewerReady(frame) {
  await frame.waitForFunction(() => {
    const viewer = window.__tabletViewer;
    return viewer?.opener.isOpen && viewer.currSlide.content.element?.naturalWidth > 0;
  });
}

async function viewerGesture(page, frame, engine, vertical = false) {
  const size = page.viewportSize();
  const start = { x: size.width * .25, y: size.height * .4 };
  const finish = vertical ? { x: start.x, y: size.height * .84 } : { x: size.width * .82, y: start.y };
  if (engine === "chromium") {
    const cdp = await page.context().newCDPSession(page);
    try {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ ...start, id: 1 }] });
      for (let index = 1; index <= 8; index++) {
        await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: start.x + (finish.x - start.x) * index / 8, y: start.y + (finish.y - start.y) * index / 8, id: 1 }] });
        await frames(frame, 1);
      }
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } finally { await cdp.detach(); }
  } else {
    for (let index = 0; index <= 9; index++) {
      await frame.evaluate(({ start, finish, index }) => {
        const viewer = window.__tabletViewer;
        const target = index === 0 ? viewer.currSlide.content.element : window;
        target.dispatchEvent(new PointerEvent(index === 0 ? "pointerdown" : index === 9 ? "pointerup" : "pointermove", {
          bubbles: true, cancelable: true, pointerId: 72, pointerType: "touch", isPrimary: true,
          button: 0, buttons: index === 9 ? 0 : 1,
          clientX: start.x + (finish.x - start.x) * Math.min(index, 8) / 8,
          clientY: start.y + (finish.y - start.y) * Math.min(index, 8) / 8,
        }));
      }, { start, finish, index });
      await frames(frame, 1);
    }
  }
}

async function run(browser, engine, viewport, hasTouch) {
  const name = `${engine}-${viewport.width}x${viewport.height}-${hasTouch ? "touch" : "mouse"}`;
  const page = await browser.newPage({ viewport, hasTouch });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  try {
    const list = await (await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=60&light=1`)).json();
    const group = (list.data || list).items.find(item => item.image_count > 1);
    assert.ok(group, "fixture needs a group with multiple images");
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.evaluate(() => {
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__tabletViewer = this; } };
    });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill("构图 1"); await frame.locator("#gallerySearch").press("Tab");
    await frame.locator(`[data-gallery-id="${group.id}"] .gallery-info`).click();
    await selected(frame, 0); await frame.locator("#detailCopy:not(:disabled)").waitFor();
    const expectedTouch = hasTouch || viewport.width <= 540;
    assert.equal(await frame.evaluate(() => window.ImageStudioDetailSwipe.usesTouchInteraction()), expectedTouch);
    await frame.locator("#drawerBody").evaluate(element => { element.scrollTop = 0; });
    await touch(frame, "touchstart"); await touch(frame, "touchmove", -50); await frames(frame);
    if (expectedTouch) {
      const motion = await frame.locator(".detail-swipe-overlay").evaluate(element => ({
        position: getComputedStyle(element).position,
        x: new DOMMatrixReadOnly(getComputedStyle(element.querySelector(".detail-swipe-track")).transform).m41,
      }));
      assert.equal(motion.position, "absolute", "tablet needs the same swipe layer styling as phones");
      assert.ok(Math.abs(motion.x + 50) < 3, "tablet image must follow the finger");
      await page.screenshot({ path: path.join(output, `${name}-swipe.png`) });
      await touch(frame, "touchmove", -220); await touch(frame, "touchend", -220);
      await selected(frame, 1);

      // Rotating during a gesture must release its old geometry cleanly.
      await touch(frame, "touchstart"); await touch(frame, "touchmove", -40);
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      await frame.locator(".detail-swipe-overlay").waitFor({ state: "detached" });
      await touch(frame, "touchend", -40); await selected(frame, 1);
      assert.equal(await frame.evaluate(() => window.ImageStudioDetailSwipe.usesTouchInteraction()), true);
      await touch(frame, "touchstart"); await touch(frame, "touchmove", -220); await touch(frame, "touchend", -220);
      await selected(frame, 2);
    } else {
      assert.equal(await frame.locator(".detail-swipe-overlay").count(), 0);
      await touch(frame, "touchend", -50); await selected(frame, 0);
    }

    await page.waitForTimeout(550); // Swiping intentionally suppresses accidental clicks for 500 ms.
    await frame.locator("[data-detail-image]").click();
    if (expectedTouch) {
      await viewerReady(frame);
      assert.equal(await frame.locator("#imagePreview").isVisible(), false);
      await frame.evaluate(() => window.__tabletViewer.goTo(window.__tabletViewer.currIndex - 2));
      await viewerReady(frame);
      const initialIndex = await frame.evaluate(() => window.__tabletViewer.currIndex);
      await viewerGesture(page, frame, engine);
      await frame.waitForFunction(index => window.__tabletViewer.currIndex === index - 1, initialIndex);
      await viewerReady(frame);
      assert.notEqual(await frame.evaluate(() => window.__tabletViewer.currSlide.data.generation_id), group.id, "lightbox swipe should cross to the adjacent group");
      const zoom = await frame.evaluate(() => {
        const viewer = window.__tabletViewer, initial = viewer.currSlide.zoomLevels.initial;
        viewer.zoomTo(initial * 2, { x: innerWidth / 2, y: innerHeight / 2 }, 0);
        return { initial, actual: viewer.currSlide.currZoomLevel };
      });
      assert.ok(zoom.actual > zoom.initial, "tablet lightbox must support zoom");
      await frame.evaluate(() => window.__tabletViewer.zoomTo(window.__tabletViewer.currSlide.zoomLevels.initial, undefined, 0));
      await page.screenshot({ path: path.join(output, `${name}-lightbox.png`) });
      const current = await frame.evaluate(() => ({ generation: window.__tabletViewer.currSlide.data.generation_id, index: window.__tabletViewer.currSlide.data.image_index }));
      await viewerGesture(page, frame, engine, true);
      await frame.locator(".pswp--open").waitFor({ state: "detached" });
      await frame.waitForFunction(current => document.querySelector(".detail-image-frame")?.dataset.generationId === current.generation
        && document.querySelector("[data-detail-image]")?.dataset.detailImage === String(current.index), current);
      await frame.locator("#detailCopy:not(:disabled)").waitFor();
    } else {
      await frame.locator("#imagePreview:not(.is-hidden)").waitFor();
      assert.equal(await frame.locator(".pswp--open").count(), 0, "mouse desktop keeps its existing preview");
    }
    const overflow = await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert.equal(overflow, false); assert.deepEqual(errors, []);
    console.log(`${name}: detail gesture routing, rotation recovery, lightbox navigation/zoom/close and desktop fallback passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
    const browser = await engines[engine].launch({ headless: true });
    try {
      for (const viewport of [{ width: 768, height: 1024 }, { width: 1366, height: 1024 }]) await run(browser, engine, viewport, true);
      await run(browser, engine, { width: 1024, height: 768 }, false);
    } finally { await browser.close(); }
  }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
