/* Preserve the fullscreen backdrop through interrupted PhotoSwipe gestures. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const browserName = process.env.STUDIO_BROWSER || "chromium";
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function installProbe(inner) {
  await inner.evaluate(() => {
    const NativePhotoSwipe = window.PhotoSwipe;
    window.__opacitySamples = [];
    window.__opacityPhase = "entry";
    window.PhotoSwipe = class extends NativePhotoSwipe {
      constructor(options) { super(options); window.__opacityViewer = this; }
      init() {
        const result = super.init();
        this.on("moveMainScroll", event => {
          if (event.dragging && this.gestures.dragAxis === "x") window.__opacityMovedPhase = window.__opacityPhase;
        });
        const sample = () => {
          if (!this.element?.isConnected) return;
          window.__opacitySamples.push({
            phase: window.__opacityPhase, opacity: this.bgOpacity,
            cssOpacity: Number(getComputedStyle(this.bg).opacity),
            axis: this.gestures.dragAxis, dragging: this.gestures.isDragging,
            moved: window.__opacityMovedPhase === window.__opacityPhase,
            closing: this.isDestroying || this.opener.isClosing,
          });
          requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
        return result;
      }
    };
    // Dispatch through PhotoSwipe's real pointer handlers in both browser
    // engines, rather than directly changing its gesture or opacity state.
    window.__opacityPointer = (type, x, y) => {
      const viewer = window.__opacityViewer;
      viewer.scrollWrap.dispatchEvent(new PointerEvent(type, {
        pointerId: 71, pointerType: "touch", isPrimary: true,
        bubbles: true, cancelable: true, clientX: x, clientY: y,
        buttons: /up|cancel/.test(type) ? 0 : 1, button: 0,
      }));
    };
  });
}

async function pointer(inner, type, x, y, frames = 1) {
  await inner.evaluate(async ({ type, x, y, frames }) => {
    window.__opacityPointer(type, x, y);
    for (let frame = 0; frame < frames; frame++) await new Promise(requestAnimationFrame);
  }, { type, x, y, frames });
}

async function phase(inner, name) { await inner.evaluate(name => { window.__opacityPhase = name; }, name); }

async function settled(inner) {
  await inner.waitForFunction(() => {
    const viewer = window.__opacityViewer;
    return viewer?.opener.isOpen && !viewer.isDestroying
      && !viewer.gestures.isDragging && !viewer.gestures.isZooming
      && !viewer.mainScroll.isShifted() && !viewer.animations.activeAnimations.length
      && viewer.bgOpacity === 1 && Number(getComputedStyle(viewer.bg).opacity) === 1;
  });
}

async function horizontal(inner, { alreadyDown = false } = {}) {
  const initial = await inner.evaluate(() => window.__opacityViewer.currIndex);
  if (!alreadyDown) await pointer(inner, "pointerdown", 320, 400);
  for (let step = 1; step <= 6; step++) await pointer(inner, "pointermove", 320 - 42 * step, 400, 2);
  await pointer(inner, "pointerup", 68, 400);
  await inner.waitForFunction(index => window.__opacityViewer.currIndex === index + 1, initial);
  await settled(inner);
}

async function holdVertical(page, inner) {
  await pointer(inner, "pointerdown", 210, 400);
  await pointer(inner, "pointermove", 210, 415, 2);
  await pointer(inner, "pointermove", 210, 480, 2);
  // Remove flick velocity so releasing starts a rebound rather than closing.
  await page.waitForTimeout(180);
  const held = await inner.evaluate(() => ({ opacity: window.__opacityViewer.bgOpacity, axis: window.__opacityViewer.gestures.dragAxis }));
  assert.equal(held.axis, "y");
  assert.ok(held.opacity < .95, "vertical dismissal must still reveal the underlying detail");
}

async function assertHorizontalFrames(inner, name) {
  const samples = await inner.evaluate(name => window.__opacitySamples.filter(sample => sample.phase === name && sample.moved && !sample.closing), name);
  assert.ok(samples.length >= 4, `${name}: expected multiple real horizontal gesture frames`);
  for (const sample of samples) {
    assert.equal(sample.opacity, 1, `${name}: horizontal motion must retain full parent coverage`);
    assert.equal(sample.cssOpacity, 1, `${name}: the actual background layer must stay opaque`);
  }
}

(async () => {
  const browser = await playwright[browserName].launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
    page.setDefaultTimeout(12000);
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    const inner = page.frames().find(item => item.url().includes("/ui/"));
    await installProbe(inner);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await inner.locator("[data-detail-image]").evaluate(image => image.click());
    await settled(inner);

    await phase(inner, "horizontal");
    await horizontal(inner);
    await assertHorizontalFrames(inner, "horizontal");

    await phase(inner, "vertical-rebound");
    await holdVertical(page, inner);
    // Start the new pointer in the same turn so the previous spring cannot
    // complete between automation round trips on a busy test machine.
    await inner.evaluate(() => {
      window.__opacityPointer("pointerup", 210, 480);
      window.__opacityPointer("pointerdown", 320, 400);
      window.__opacityPhase = "interrupted-horizontal";
    });
    await horizontal(inner, { alreadyDown: true });
    await assertHorizontalFrames(inner, "interrupted-horizontal");
    await page.waitForTimeout(150);
    await settled(inner);

    await phase(inner, "cancelled-vertical");
    await holdVertical(page, inner);
    await pointer(inner, "pointercancel", 210, 480);
    await settled(inner);

    await phase(inner, "rebound-interrupted-by-tap");
    await holdVertical(page, inner);
    await inner.evaluate(() => {
      window.__opacityPointer("pointerup", 210, 480);
      window.__opacityPointer("pointerdown", 210, 400);
      window.__opacityPointer("pointerup", 210, 400);
    });
    await settled(inner);

    await phase(inner, "vertical-close");
    await pointer(inner, "pointerdown", 210, 350);
    for (let step = 1; step <= 7; step++) await pointer(inner, "pointermove", 210, 350 + 40 * step, 2);
    const closingOpacity = await inner.evaluate(() => window.__opacityViewer.bgOpacity);
    assert.ok(closingOpacity < .7, "a deliberate vertical dismissal must still fade the background");
    await pointer(inner, "pointerup", 210, 630, 0);
    await inner.locator(".pswp").waitFor({ state: "detached" });
    assert.deepEqual(errors, []);
    await page.close();
    console.log(`${browserName}: horizontal coverage, interrupted vertical rebound, cancel/tap recovery and vertical dismissal passed`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
