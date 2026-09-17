/* Native image menus must not leave PhotoSwipe with a phantom second finger. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const browserName = process.env.STUDIO_BROWSER || "chromium";
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function installProbe(inner) {
  await inner.evaluate(() => {
    const NativePhotoSwipe = window.PhotoSwipe;
    window.__menuViewers = [];
    window.__menuTapActions = 0;
    window.PhotoSwipe = class extends NativePhotoSwipe {
      constructor(options) {
        super(options);
        window.__menuViewer = this;
        window.__menuViewers.push(this);
        this.on("tapAction", () => { window.__menuTapActions++; });
        this.on("doubleTapAction", () => { window.__menuTapActions++; });
      }
    };
    window.__menuPointer = (type, x, y, id = 71, primary = true, atWindow = false) => {
      const target = atWindow ? window : window.__menuViewer.container;
      target.dispatchEvent(new PointerEvent(type, {
        pointerId: id, pointerType: "touch", isPrimary: primary,
        bubbles: true, cancelable: true, clientX: x, clientY: y,
        buttons: /up|cancel/.test(type) ? 0 : 1, button: 0,
      }));
    };
    window.__menuContext = () => {
      const viewer = window.__menuViewer;
      const target = viewer.currSlide.content.element || viewer.scrollWrap;
      const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      target.dispatchEvent(event);
      return event.defaultPrevented;
    };
  });
}

async function pointer(inner, type, x, y, { id = 71, primary = true, frames = 1, atWindow = false } = {}) {
  await inner.evaluate(async args => {
    window.__menuPointer(args.type, args.x, args.y, args.id, args.primary, args.atWindow);
    for (let frame = 0; frame < args.frames; frame++) await new Promise(requestAnimationFrame);
  }, { type, x, y, id, primary, frames, atWindow });
}

async function settled(inner) {
  await inner.waitForFunction(() => {
    const viewer = window.__menuViewer;
    return viewer?.opener.isOpen && !viewer.isDestroying
      && !viewer.gestures.isDragging && !viewer.gestures.isZooming
      && !viewer.mainScroll.isShifted() && !viewer.animations.activeAnimations.length
      && viewer.bgOpacity === 1;
  });
}

async function assertSingleContact(inner, message) {
  const state = await inner.evaluate(() => ({
    multi: window.__menuViewer.gestures.isMultitouch,
    zooming: window.__menuViewer.gestures.isZooming,
  }));
  assert.deepEqual(state, { multi: false, zooming: false }, message);
}

async function contextMenu(inner) {
  assert.equal(await inner.evaluate(() => window.__menuContext()), false,
    "the plugin must preserve the native image menu");
  await settled(inner);
  assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.raf), null,
    "interruption must stop the old gesture animation loop");
}

async function assertUnchanged(inner, before, message) {
  await settled(inner);
  const after = await inner.evaluate(() => ({
    index: window.__menuViewer.currIndex,
    taps: window.__menuTapActions,
    zoom: window.__menuViewer.currSlide.currZoomLevel,
  }));
  assert.equal(after.index, before.index, `${message}: must not turn the page`);
  assert.equal(after.taps, before.taps, `${message}: must not become a tap or double tap`);
  if (before.zoom !== undefined) assert.ok(Math.abs(after.zoom - before.zoom) < .0001,
    `${message}: preserve the user's valid zoom`);
}

async function snapshot(inner, zoom = false) {
  return inner.evaluate(zoom => ({
    index: window.__menuViewer.currIndex, taps: window.__menuTapActions,
    ...(zoom ? { zoom: window.__menuViewer.currSlide.currZoomLevel } : {}),
  }), zoom);
}

async function horizontal(inner, { alreadyDown = false, id = 71 } = {}) {
  const initial = await inner.evaluate(() => window.__menuViewer.currIndex);
  if (!alreadyDown) await pointer(inner, "pointerdown", 320, 400, { id });
  await assertSingleContact(inner, "a fresh single finger must not combine with a stale contact");
  for (let step = 1; step <= 6; step++) await pointer(inner, "pointermove", 320 - 42 * step, 400, { id, frames: 2 });
  assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.isZooming), false);
  await pointer(inner, "pointerup", 68, 400, { id });
  await inner.waitForFunction(index => window.__menuViewer.currIndex === index + 1, initial);
  await settled(inner);
  await inner.waitForFunction(() => {
    const viewer = window.__menuViewer;
    const item = viewer.options.dataSource[viewer.currIndex];
    const backdrop = viewer.element.querySelector("canvas.image-studio-viewer-backdrop");
    return !!item.displaySrc && viewer.currSlide.content.element?.src === item.displaySrc && backdrop?.dataset.backdropState === "idle"
      && backdrop.width > 1 && backdrop.height > 1 && backdrop.dataset.previewSource === item.previewSrc;
  });
}

async function resetView(inner) {
  await inner.evaluate(() => {
    const viewer = window.__menuViewer;
    viewer.goTo(0);
    viewer.zoomTo(viewer.currSlide.zoomLevels.initial, undefined, 0);
  });
  await settled(inner);
}

(async () => {
  const browser = await playwright[browserName].launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
    page.setDefaultTimeout(12000);
    const errors = [], originalRequests = []; page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname.includes("/gallery/image/") && url.searchParams.get("detail") === "original") originalRequests.push(url.pathname);
    });
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    const inner = page.frames().find(item => item.url().includes("/ui/"));
    await installProbe(inner);
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card .gallery-info").first().click();
    await inner.locator("[data-detail-image]").evaluate(image => image.click());
    await settled(inner);

    // Safari can hand the press to its native menu without delivering pointerup.
    const idle = await snapshot(inner);
    await pointer(inner, "pointerdown", 210, 400);
    await contextMenu(inner);
    await assertUnchanged(inner, idle, "native menu interruption");
    await horizontal(inner, { id: 72 });
    assert.equal(originalRequests.length, 0, "menu recovery and a normal swipe must remain independent of original-image loading");

    // Some iOS callout paths do not emit contextmenu either. A new primary
    // contact is the recovery boundary, including reuse of the old pointer ID.
    for (const newId of [74, 73]) {
      await resetView(inner);
      await inner.evaluate(newId => {
        window.__menuPointer("pointerdown", 210, 400, 73);
        window.__menuPointer("pointerdown", 320, 400, newId);
      }, newId);
      await horizontal(inner, { alreadyDown: true, id: newId });
    }

    // A real second finger is not a fresh primary contact and must still pinch.
    await resetView(inner);
    const initialZoom = await inner.evaluate(() => window.__menuViewer.currSlide.currZoomLevel);
    await pointer(inner, "pointerdown", 155, 400, { id: 81 });
    await pointer(inner, "pointerdown", 235, 400, { id: 82, primary: false });
    assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.isMultitouch), true);
    for (let step = 1; step <= 4; step++) {
      await pointer(inner, "pointermove", 155 - 12 * step, 400, { id: 81, frames: 2 });
      await pointer(inner, "pointermove", 235 + 12 * step, 400, { id: 82, primary: false, frames: 2 });
    }
    assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.isZooming), true);
    assert.ok(await inner.evaluate(initial => window.__menuViewer.currSlide.currZoomLevel > initial * 1.4, initialZoom));
    const pinched = await snapshot(inner, true);
    await contextMenu(inner);
    await assertUnchanged(inner, pinched, "interrupted valid pinch");
    await pointer(inner, "pointerdown", 190, 400, { id: 83 });
    await pointer(inner, "pointermove", 220, 400, { id: 83, frames: 2 });
    await assertSingleContact(inner, "one-finger pan after the menu must retain the pinch zoom");
    await contextMenu(inner);
    await assertUnchanged(inner, pinched, "interrupted zoomed pan");

    // Cancellation is not release: even a drag beyond the normal turn/close
    // threshold must neither fling to another image nor dismiss the viewer.
    for (const axis of ["x", "y"]) {
      await resetView(inner);
      const before = await snapshot(inner);
      await pointer(inner, "pointerdown", 320, 350);
      for (let step = 1; step <= 6; step++) await pointer(inner, "pointermove",
        axis === "x" ? 320 - 42 * step : 320,
        axis === "y" ? 350 + 42 * step : 350, { frames: 2 });
      assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.dragAxis), axis);
      if (axis === "y") assert.ok(await inner.evaluate(() => window.__menuViewer.bgOpacity < .7));
      await contextMenu(inner);
      await assertUnchanged(inner, before, `interrupted ${axis} drag`);
      await horizontal(inner);
    }

    for (const multi of [false, true]) {
      await resetView(inner);
      const before = await snapshot(inner);
      await pointer(inner, "pointerdown", 210, 350, { id: 91 });
      if (multi) {
        await pointer(inner, "pointerdown", 260, 350, { id: 92, primary: false });
        await pointer(inner, "pointermove", 300, 350, { id: 92, primary: false, frames: 2 });
        assert.equal(await inner.evaluate(() => window.__menuViewer.gestures.isZooming), true);
      } else {
        for (let step = 1; step <= 6; step++) await pointer(inner, "pointermove", 210, 350 + 42 * step, { id: 91, frames: 2 });
        assert.ok(await inner.evaluate(() => window.__menuViewer.bgOpacity < .7));
      }
      await pointer(inner, "pointercancel", 210, 602, { id: 91 });
      await assertUnchanged(inner, before, `viewer cancellation during ${multi ? "pinch" : "vertical drag"}`);
      await pointer(inner, "pointerdown", 210, 400, { id: 93 });
      await pointer(inner, "pointermove", 230, 400, { id: 93, frames: 2 });
      await assertSingleContact(inner, "cancelling either pinch contact must remove both old contacts");
      await contextMenu(inner);
    }

    // The system can also send cancellation outside the viewer or suspend the
    // window entirely; the next gesture must start with a clean contact set.
    for (const kind of ["window-cancel", "blur", "pagehide"]) {
      await resetView(inner);
      const before = await snapshot(inner);
      await pointer(inner, "pointerdown", 210, 400);
      if (kind === "window-cancel") await pointer(inner, "pointercancel", 210, 400, { atWindow: true });
      else await inner.evaluate(kind => window.dispatchEvent(new Event(kind)), kind);
      await assertUnchanged(inner, before, kind);
      if (kind !== "window-cancel") await inner.evaluate(kind => window.dispatchEvent(new Event(kind === "blur" ? "focus" : "pageshow")), kind);
      await horizontal(inner);
    }

    // Listeners belong to each viewer instance; exiting with a lost contact
    // must not poison a later instance or leave stale window listeners active.
    await pointer(inner, "pointerdown", 210, 400);
    await contextMenu(inner);
    await inner.evaluate(() => window.__menuViewer.close());
    await inner.locator(".pswp").waitFor({ state: "detached" });
    await inner.locator("[data-detail-image]").evaluate(image => image.click());
    await settled(inner);
    assert.equal(await inner.evaluate(() => window.__menuViewers.length), 2);
    await resetView(inner);
    await pointer(inner, "pointerdown", 210, 400);
    await contextMenu(inner);
    await horizontal(inner);

    // Recovery must also retain intentional double-tap zoom, rather than
    // disabling all taps as a workaround for the native-menu interruption.
    await resetView(inner);
    const beforeDoubleTap = await snapshot(inner, true);
    for (let tap = 0; tap < 2; tap++) {
      await pointer(inner, "pointerdown", 195, 400, { id: 95 });
      await pointer(inner, "pointerup", 195, 400, { id: 95 });
    }
    await settled(inner);
    assert.ok(await inner.evaluate(zoom => window.__menuViewer.currSlide.currZoomLevel > zoom, beforeDoubleTap.zoom));
    assert.equal(await inner.evaluate(() => window.__menuTapActions), beforeDoubleTap.taps + 1);
    await inner.waitForFunction(() => {
      const slide = window.__menuViewer.currSlide;
      return slide.data.originalSrc?.startsWith("blob:") && slide.content.element?.src === slide.data.originalSrc
        && slide.content.element.complete && slide.content.element.naturalWidth > 1;
    });
    assert.ok(originalRequests.length > 0, "intentional zoom must still request the original after native-menu recovery");
    assert.deepEqual(errors, []);
    await page.close();
    console.log(`${browserName}: native menu, lost/reused contacts, pinch/double tap, preserved zoom, interrupted drags, window cancellation, image queue recovery and reopen passed`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
