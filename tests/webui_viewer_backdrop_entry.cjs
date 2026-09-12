/* Observe the first fullscreen frame, with preview decoding and originals held. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const browserName = process.env.STUDIO_BROWSER || "chromium";
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function installEntryProbe(inner) {
  await inner.evaluate(() => {
    const nativeDecode = HTMLImageElement.prototype.decode;
    window.__entryGateMode = "hold"; window.__entryGateCalls = 0; window.__entrySamples = [];
    HTMLImageElement.prototype.decode = function () {
      if (this.className !== "image-studio-viewer-backdrop") return nativeDecode.call(this);
      window.__entryGateCalls++;
      if (window.__entryGateMode === "fail") return Promise.reject(new DOMException("Synthetic decode failure", "EncodingError"));
      if (window.__entryGateMode === "hold") return new Promise((resolve) => { window.__entryRelease = () => { window.__entryGateMode = "ready"; resolve(); }; }).then(() => nativeDecode.call(this));
      return nativeDecode.call(this);
    };
    const snapshot = (viewer) => {
      const image = viewer.bg.querySelector("canvas.image-studio-viewer-backdrop");
      return {
        loaded: !!image && image.width > 1 && image.height > 1 && image.getContext("2d").getImageData(0, 0, 1, 1).data[3] === 255,
        width: image?.width, height: image?.height,
        source: image?.dataset.previewSource, opacity: image ? Number(getComputedStyle(image).opacity) : null,
        visibility: image ? getComputedStyle(image).visibility : null,
        holderOpacity: image ? Number(getComputedStyle(image.parentElement).opacity) : null,
        holderFilter: image ? getComputedStyle(image.parentElement).filter : null,
        backgroundAnimations: image?.getAnimations().length,
        original: !!viewer.currSlide.data.originalSrc,
        open: viewer.opener.isOpen, state: image?.dataset.backdropState,
      };
    };
    const Original = window.PhotoSwipe;
    window.PhotoSwipe = class extends Original {
      constructor(options) {
        super(options); window.__entryViewer = this;
        this.on("pointerDown", () => { window.__entryPointerEvents = (window.__entryPointerEvents || 0) + 1; });
      }
      init() {
        const result = super.init();
        window.__entryInitial = snapshot(this);
        window.__entryInitial.entryAnimation = this.element.getAnimations().some((animation) => animation.playState === "running" && Number(animation.effect?.getTiming().duration) > 1);
        const before = window.__entryPointerEvents || 0;
        for (const type of ["pointerdown", "pointerup"]) this.scrollWrap.dispatchEvent(new PointerEvent(type, { pointerId: 31, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: 195, clientY: 400, buttons: type === "pointerdown" ? 1 : 0, button: 0 }));
        window.__entryInitial.firstPointerAccepted = window.__entryPointerEvents > before;
        const sample = () => {
          if (!this.element?.isConnected || window.__entrySamples.length >= 14) return;
          window.__entrySamples.push(snapshot(this)); requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
        return result;
      }
    };
  });
}

async function resetProbe(inner, mode) {
  await inner.evaluate((mode) => { window.__entryGateMode = mode; window.__entryGateCalls = 0; window.__entryInitial = null; window.__entrySamples = []; }, mode);
}

function readyBackground(sample, source, label) {
  assert.equal(sample.loaded, true, `${label}: first visible background must already be loaded`);
  assert.equal(sample.source, source, `${label}: background must use the thumbnail`);
  assert.equal(sample.opacity, 1, `${label}: background image must completely cover its layer`);
  assert.equal(sample.visibility, "visible", `${label}: legacy image src selectors must not hide the canvas`);
  assert.equal(sample.holderOpacity, 1, `${label}: the canvas already contains the complete opaque background`);
  assert.equal(sample.holderFilter, "none", `${label}: fullscreen does not require a live CSS filter`);
  assert.ok(Math.max(sample.width, sample.height) <= 320, `${label}: the background bitmap must stay bounded`);
  assert.equal(sample.backgroundAnimations, 0, `${label}: do not fade in from an empty background`);
  assert.ok(["idle", "paused"].includes(sample.state), `${label}: entry must not wait for background loading or fading`);
  assert.equal(sample.open, true);
}

(async () => {
  const browser = await playwright[browserName].launch({ headless: true });
  try {
    for (const reduced of [false, true]) {
      const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, reducedMotion: reduced ? "reduce" : "no-preference" });
      page.setDefaultTimeout(12000);
      const errors = []; page.on("pageerror", error => errors.push(error.message));
      let releaseOriginals;
      const originalGate = new Promise(resolve => { releaseOriginals = resolve; });
      const delayOriginal = async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.includes("/gallery/assets/") || url.searchParams.get("detail") === "original") await originalGate;
        try { await route.continue(); } catch (error) { if (!/closed|handled|disposed/i.test(error.message)) throw error; }
      };
      try {
        await page.route("**/gallery/assets/**", delayOriginal);
        await page.route("**/gallery/image/**", delayOriginal);
        await page.goto(base); const frame = page.frameLocator("#studio");
        await frame.locator("#modelChoice:not(:disabled)").waitFor();
        const inner = page.frames().find(item => item.url().includes("/ui/"));
        await frame.locator('[data-view="gallery"]').click();
        await frame.locator(".gallery-card .gallery-info").first().click();
        await inner.waitForFunction(() => {
          const image = document.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
          return image?.complete && image.naturalWidth > 1 && image.dataset.backdropState === "idle";
        });
        const source = await inner.locator(".detail-image-backdrop:not(.detail-backdrop-previous)").getAttribute("src");
        await installEntryProbe(inner);
        await inner.locator("[data-detail-image]").evaluate(image => image.click());
        await inner.waitForFunction(() => window.__entryGateCalls > 0);
        await page.waitForTimeout(250);
        assert.equal(await inner.locator(".pswp").count(), 0, "a delayed initial preview must not expose an empty fullscreen background");
        await inner.evaluate(() => window.__entryRelease());
        await inner.waitForFunction(() => !!window.__entryInitial);
        let initial = await inner.evaluate(() => window.__entryInitial);
        readyBackground(initial, source, "decoded entry");
        assert.equal(initial.original, false, "entry must not wait for the original response");
        assert.equal(initial.entryAnimation, !reduced, "the visual entry animation still follows reduced-motion preference");
        assert.equal(initial.firstPointerAccepted, true, "the first pointer must be accepted during the visual entry animation");
        await inner.waitForFunction(() => window.__entrySamples.length >= 10);
        for (const sample of await inner.evaluate(() => window.__entrySamples)) readyBackground(sample, source, "entry frame");

        for (const mode of ["fail", "hold"]) {
          await inner.evaluate(() => window.__entryViewer.close()); await inner.locator(".pswp").waitFor({ state: "detached" });
          await resetProbe(inner, mode);
          await inner.locator("[data-detail-image]").evaluate(image => image.click());
          await inner.waitForFunction(() => !!window.__entryInitial, null, { timeout: 2000 });
          initial = await inner.evaluate(() => window.__entryInitial);
          readyBackground(initial, source, `${mode} fallback`);
          assert.equal(initial.firstPointerAccepted, true);
          assert.equal(initial.original, false);
          if (mode === "hold") await inner.evaluate(() => window.__entryRelease());
        }

        await inner.evaluate(() => window.__entryViewer.close()); await inner.locator(".pswp").waitFor({ state: "detached" });
        await resetProbe(inner, "hold");
        await inner.locator("[data-detail-image]").evaluate(image => image.click());
        await inner.waitForFunction(() => window.__entryGateCalls > 0);
        await frame.locator("#closeDrawer").click();
        await inner.evaluate(() => window.__entryRelease());
        await page.waitForTimeout(200);
        assert.equal(await inner.locator(".pswp").count(), 0, "a stale decode must not open fullscreen after detail closed");
        assert.deepEqual(errors, []);
        console.log(`${browserName} ${reduced ? "reduced" : "animated"}: prepared first frame, delayed original, decode fallback, immediate pointer and stale-open guards passed`);
      } finally { releaseOriginals(); await page.close(); }
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
