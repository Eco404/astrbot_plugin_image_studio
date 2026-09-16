/* Backdrop lifecycle recovery at animation and decoded-image boundaries. */
const assert = require("node:assert/strict");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const requestedBrowser = process.env.STUDIO_BROWSER;
const root = path.resolve(__dirname, "../../pages/image-studio");

async function fixture(page, surface) {
  await page.setContent('<style>main{position:relative;width:160px;height:160px;isolation:isolate}img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}</style><main></main>');
  await page.addScriptTag({ path: path.join(root, "backdrop.js") });
  await page.evaluate(surface => {
    const nativeAnimate = Element.prototype.animate;
    window.animations = [];
    window.pauseNext = 0;
    Element.prototype.animate = function (...args) {
      const animation = nativeAnimate.apply(this, args);
      window.animations.push(animation);
      if (window.pauseNext > 0) {
        window.pauseNext--;
        animation.pause(); animation.currentTime = 0;
      }
      return animation;
    };
    const colors = { blue: "#235daa", green: "#249c58", red: "#bd4134", purple: "#914bba" };
    window.sources = Object.fromEntries(Object.entries(colors).map(([key, color]) => {
      const canvas = document.createElement("canvas"); canvas.width = canvas.height = 128;
      const context = canvas.getContext("2d"); context.fillStyle = color; context.fillRect(0, 0, 128, 128);
      return [key, canvas.toDataURL()];
    }));
    window.options = { opacity: surface === "detail" ? .62 : .64, previousClass: surface === "detail" ? "detail-backdrop-previous" : "image-studio-viewer-backdrop-previous" };
    window.ownerClass = surface === "detail" ? "detail-image-backdrop" : "image-studio-viewer-backdrop";
    window.fadeCount = CSS.supports("mix-blend-mode", "plus-lighter") ? 2 : 1;
    window.reset = async () => {
      if (window.owner) window.ImageStudioBackdrop.dispose(window.owner);
      const owner = new Image(); owner.className = window.ownerClass;
      document.querySelector("main").replaceChildren(owner);
      window.owner = owner; window.animations = []; window.jobs = {}; window.results = {}; window.pauseNext = 0;
      owner.src = window.sources.blue; await owner.decode();
      if (!window.ImageStudioBackdrop.seed(owner, owner.src, window.options)) throw new Error("Fixture seed failed");
    };
    window.start = (key, color) => {
      window.jobs[key] = window.ImageStudioBackdrop.transition(window.owner, window.sources[color], window.options).then(result => {
        window.results[key] = result; return result;
      });
    };
  }, surface);
}

async function reset(page) { await page.evaluate(() => window.reset()); }
async function start(page, key, color) { await page.evaluate(({ key, color }) => window.start(key, color), { key, color }); }

async function ready(page, key, color) {
  await page.waitForFunction(({ key, color }) => window.results[key] !== undefined
    && window.owner.dataset.backdropState === "idle" && window.owner.src === window.sources[color], { key, color }, { timeout: 3500 });
  const state = await page.evaluate(key => ({
    result: window.results[key], images: document.querySelector("main").children.length,
    loaded: window.owner.complete && window.owner.naturalWidth > 0,
    opacity: Number(getComputedStyle(window.owner).opacity), animations: window.owner.getAnimations().length,
  }), key);
  assert.deepEqual(state, { result: true, images: 1, loaded: true, opacity: 1, animations: 0 });
}

async function controlledFade(page) {
  await page.evaluate(() => { window.pauseNext = window.fadeCount; window.start("first", "green"); });
  await page.waitForFunction(() => window.animations.length === window.fadeCount);
}

async function followingImage(page) {
  await start(page, "following", "purple");
  await ready(page, "following", "purple");
}

async function verify(engine, browserName) {
  const browser = await engine.launch({ headless: true });
  try {
    for (const reduced of [false, true]) {
      for (const surface of ["detail", "viewer"]) {
        const page = await browser.newPage({ reducedMotion: reduced ? "reduce" : "no-preference" });
        const errors = []; page.on("pageerror", error => errors.push(error.message));
        await fixture(page, surface);

        // A paused WAAPI animation positioned exactly at its end may never
        // resolve finished. Later image requests must still make progress.
        await reset(page); await controlledFade(page);
        const boundary = await page.evaluate(() => {
          for (const animation of window.animations) animation.currentTime = animation.effect.getTiming().duration;
          window.ImageStudioBackdrop.pause(window.owner);
          return { duration: window.animations[0].effect.getTiming().duration, phase: window.owner.dataset.backdropState, states: window.animations.map(animation => animation.playState) };
        });
        assert.equal(boundary.duration, reduced ? 140 : 280);
        assert.equal(boundary.phase, "paused");
        assert.ok(boundary.states.every(state => state === "paused"));
        await start(page, "latest", "red"); await ready(page, "latest", "red");
        assert.equal(await page.evaluate(() => window.results.first), false);
        await followingImage(page);

        // Genuine user pause must suspend the active-time watchdog, then a
        // different requested image should finish after the retained blend.
        await reset(page); await controlledFade(page);
        await page.evaluate(() => {
          for (const animation of window.animations) animation.currentTime = animation.effect.getTiming().duration / 2;
          window.ImageStudioBackdrop.pause(window.owner);
        });
        await page.waitForTimeout(450);
        assert.equal(await page.evaluate(() => window.owner.dataset.backdropState), "paused");
        assert.equal(await page.locator("main img").count(), 2, "paused blend retains its already decoded cover");
        await start(page, "latest", "red"); await ready(page, "latest", "red");
        await followingImage(page);

        // Browser cancellation rejects animation.finished. It must settle
        // the handoff instead of leaving all later transitions waiting.
        await reset(page); await controlledFade(page);
        await page.evaluate(() => { window.animations[0].cancel(); });
        await ready(page, "first", "green");
        await followingImage(page);

        // Only the stable image's second decode is hung; candidate decoding
        // remains native and the matching loaded resource can finish by TTL.
        await reset(page);
        await page.evaluate(() => {
          window.decodeCalls = 0;
          window.owner.decode = () => { window.decodeCalls++; return new Promise(() => {}); };
          window.decodeStartedAt = performance.now(); window.start("hung", "green");
        });
        await page.waitForFunction(() => window.decodeCalls > 0);
        await ready(page, "hung", "green");
        assert.ok(await page.evaluate(() => performance.now() - window.decodeStartedAt) >= 750, "hung stable decode should exercise its bounded wait");
        await page.evaluate(() => { delete window.owner.decode; });
        await followingImage(page);

        // Late callbacks from disposed/detached surfaces cannot revive a
        // candidate or roll a replacement surface back to an obsolete src.
        for (const detach of [false, true]) {
          await reset(page);
          await page.evaluate(() => {
            const nativeDecode = HTMLImageElement.prototype.decode;
            window.owner.decode = function () {
              return nativeDecode.call(this).then(() => new Promise(resolve => { window.releaseDecode = resolve; }));
            };
            window.start("obsolete", "green");
          });
          await page.waitForFunction(() => typeof window.releaseDecode === "function");
          await page.evaluate(detach => {
            if (detach) window.owner.remove();
            else window.ImageStudioBackdrop.dispose(window.owner);
          }, detach);
          await page.waitForFunction(() => window.results.obsolete !== undefined);
          assert.equal(await page.evaluate(() => window.results.obsolete), false);
          const retained = await page.evaluate(() => ({ src: window.owner.src, count: document.querySelector("main").children.length }));
          await page.evaluate(() => { window.releaseDecode(); delete window.owner.decode; });
          await page.waitForTimeout(180);
          assert.deepEqual(await page.evaluate(() => ({ src: window.owner.src, count: document.querySelector("main").children.length })), retained);
          assert.equal(retained.count, detach ? 0 : 1);
          assert.equal(await page.locator(`.${surface === "detail" ? "detail-backdrop-previous" : "image-studio-viewer-backdrop-previous"}`).count(), 0);
          await page.evaluate(() => { window.releaseDecode = null; });
        }
        assert.deepEqual(errors, []);
        await page.close();
        console.log(`${browserName} ${surface} ${reduced ? "reduced" : "animated"}: terminal pause, active pause, cancellation, decode timeout and stale disposal passed`);
      }
    }
  } finally { await browser.close(); }
}

(async () => {
  for (const name of requestedBrowser ? [requestedBrowser] : ["chromium", "webkit"]) await verify(playwright[name], name);
})().catch(error => { console.error(error); process.exitCode = 1; });
