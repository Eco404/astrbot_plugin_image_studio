/* Test the fullscreen backdrop's real canvas pixels and interruption lifecycle. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const root = require("../support/webui_paths.cjs").frontend;
const css = ["app.css", "library.css"].map(file => fs.readFileSync(path.join(root, file), "utf8")).join("\n");
const engines = process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"];

async function fixture(page) {
  await page.setContent(`<style>${css}
    .image-studio-pswp,.pswp__bg{position:fixed;inset:0}
  </style><div class="image-studio-pswp"><div class="pswp__bg"><div class="image-studio-viewer-background"></div></div></div>`);
  await page.evaluate(() => {
    // Control the scheduling boundary, while all decoding, blur rasterization,
    // canvas compositing and pixel readback remain real browser operations.
    window.clock = performance.now(); window.rafs = new Map(); let nextId = 0;
    Object.defineProperty(performance, "now", { value: () => window.clock });
    window.requestAnimationFrame = callback => { window.rafs.set(++nextId, callback); return nextId; };
    window.cancelAnimationFrame = id => window.rafs.delete(id);
    window.step = elapsed => {
      window.clock += elapsed;
      const callbacks = [...window.rafs.values()]; window.rafs.clear();
      for (const callback of callbacks) callback(window.clock);
    };
    const solid = (color, pattern = false) => {
      const canvas = document.createElement("canvas"); canvas.width = canvas.height = 96;
      const context = canvas.getContext("2d"); context.fillStyle = color; context.fillRect(0, 0, 96, 96);
      if (pattern) {
        context.clearRect(0, 0, 34, 34);
        context.fillStyle = "rgba(190,130,70,.4)"; context.fillRect(32, 32, 64, 64);
      }
      return canvas.toDataURL();
    };
    window.sources = {
      dark: solid("#182838"), light: solid("#d9b790"), green: solid("#35ad68"),
      gray: solid("#808080"),
      opaque: solid("#456ba2"), alpha: solid("rgba(75,110,175,.45)", true),
    };
    window.gates = new Map();
    const nativeDecode = HTMLImageElement.prototype.decode;
    HTMLImageElement.prototype.decode = function () {
      const gate = this.className === "image-studio-viewer-backdrop-source" && window.gates.get(this.src);
      if (!gate) return nativeDecode.call(this);
      gate.calls++;
      return nativeDecode.call(this).then(() => { gate.nativeReady = true; return gate.promise; });
    };
    window.arm = source => {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); promise.catch(() => {});
      window.gates.set(source, { promise, resolve, reject, calls: 0, nativeReady: false });
    };
    window.release = (source, fail = false) => {
      const gate = window.gates.get(source); window.gates.delete(source);
      if (fail) gate.reject(new DOMException("Controlled decode failure", "EncodingError"));
      else gate.resolve();
    };
    window.pixels = () => window.owner.getContext("2d").getImageData(0, 0, window.owner.width, window.owner.height).data;
    window.capture = () => Array.from(window.pixels());
    window.maxDifference = (before, after = window.capture()) => before.reduce((maximum, value, index) => Math.max(maximum, Math.abs(value - after[index])), 0);
    window.start = (name, source) => {
      window.jobs[name] = window.ImageStudioViewerBackdrop.transition(window.owner, source).then(result => {
        window.results[name] = result; return result;
      });
    };
    window.reset = async source => {
      if (window.owner) window.ImageStudioViewerBackdrop.dispose(window.owner);
      const image = new Image(); image.src = source; await image.decode();
      const owner = window.ImageStudioViewerBackdrop.create(image);
      if (!owner) throw new Error("Canvas fixture could not be created");
      document.querySelector(".image-studio-viewer-background").replaceChildren(owner);
      window.owner = owner; window.identity = owner; window.jobs = {}; window.results = {};
    };
    window.sample = () => {
      const canvas = window.owner, pixels = window.pixels();
      let minimumAlpha = 255;
      for (let index = 3; index < pixels.length; index += 4) minimumAlpha = Math.min(minimumAlpha, pixels[index]);
      return {
        minimumAlpha, width: canvas.width, height: canvas.height,
        sameNode: canvas === window.identity, nodes: canvas.parentNode?.children.length,
        filter: getComputedStyle(canvas).filter, parentFilter: getComputedStyle(canvas.parentElement).filter,
        opacity: getComputedStyle(canvas).opacity, blend: getComputedStyle(canvas).mixBlendMode,
      };
    };
  });
  await page.addScriptTag({ path: path.join(root, "viewer-backdrop.js") });
}

async function reset(page, name = "dark") { await page.evaluate(name => window.reset(window.sources[name]), name); }
async function start(page, name, source) { await page.evaluate(({ name, source }) => window.start(name, source), { name, source }); }
async function source(page, name, suffix = "") { return page.evaluate(({ name, suffix }) => window.sources[name] + suffix, { name, suffix }); }
async function scheduled(page) { await page.waitForFunction(() => window.rafs.size > 0, null, { polling: 10 }); }
async function step(page, elapsed) { await page.evaluate(elapsed => window.step(elapsed), elapsed); }
async function finish(page, name) {
  await scheduled(page);
  await step(page, 0);
  let elapsed = 0;
  while (await page.evaluate(name => window.results[name] === undefined, name)) {
    assert.ok(elapsed < 1200, `${name}: the bounded fade must settle`);
    await step(page, 16); elapsed += 16;
  }
  assert.equal(await page.evaluate(name => window.results[name], name), true);
  return elapsed;
}
async function assertSurface(page) {
  const state = await page.evaluate(() => window.sample());
  assert.equal(state.minimumAlpha, 255, "every background pixel stays opaque, including transparent source art");
  assert.ok(state.width > 0 && state.height > 0 && Math.max(state.width, state.height) <= 320, "raster dimensions stay bounded");
  assert.equal(state.sameNode, true); assert.equal(state.nodes, 1);
  assert.equal(state.filter, "none"); assert.equal(state.parentFilter, "none");
  assert.equal(state.opacity, "1"); assert.equal(state.blend, "normal");
}

async function oldCssPixel(page) {
  // Independent browser-rendered reference for the previous visual design:
  // thumbnail opacity over the theme's page color, with the former CSS blur.
  // A flat gray source isolates brightness from differences in blur kernels.
  await page.evaluate(async () => {
    const reference = document.createElement("div"); reference.id = "old-backdrop-reference";
    reference.style.cssText = "position:fixed;z-index:9999;left:0;top:0;width:120px;height:120px;overflow:hidden;background:var(--page)";
    const image = new Image(); image.src = window.sources.gray;
    image.style.cssText = "position:absolute;inset:-40px;width:200px;height:200px;opacity:.64;filter:blur(24px) saturate(.82);transform:scale(1.05)";
    await image.decode(); reference.append(image); document.body.append(reference);
  });
  const screenshot = await page.screenshot({ animations: "allow", scale: "css", clip: { x: 0, y: 0, width: 120, height: 120 } });
  return page.evaluate(async data => {
    const image = new Image(); image.src = `data:image/png;base64,${data}`; await image.decode();
    const capture = document.createElement("canvas"); capture.width = image.naturalWidth; capture.height = image.naturalHeight;
    const context = capture.getContext("2d"); context.drawImage(image, 0, 0);
    const pixel = Array.from(context.getImageData(Math.floor(capture.width / 2), Math.floor(capture.height / 2), 1, 1).data);
    document.getElementById("old-backdrop-reference").remove();
    return pixel;
  }, screenshot.toString("base64"));
}

async function verifyThemeBrightness(page) {
  const referenceColors = {};
  await page.evaluate(() => { window.themePixels = {}; });
  for (const theme of ["light", "dark"]) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    await reset(page, "gray"); await assertSurface(page);
    const expected = await oldCssPixel(page);
    const actual = await page.evaluate(theme => {
      window.themePixels[theme] = window.capture();
      return Array.from(window.owner.getContext("2d").getImageData(Math.floor(window.owner.width / 2), Math.floor(window.owner.height / 2), 1, 1).data);
    }, theme);
    assert.ok(actual.every((value, channel) => Math.abs(value - expected[channel]) <= 2),
      `${theme}: canvas ${actual} must retain the old CSS background brightness ${expected}`);
    referenceColors[theme] = actual;
  }
  assert.ok(referenceColors.light[0] - referenceColors.dark[0] > 50,
    "the light theme must not retain the previous fixed dark canvas base");

  // The asset URL can remain unchanged when a theme changes: source-only
  // caching must not retain pixels composited against the former theme.
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await reset(page, "gray");
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await start(page, "theme-dark", await source(page, "gray")); await scheduled(page); await step(page, 0);
  let elapsed = 0;
  while (await page.evaluate(() => window.results["theme-dark"] === undefined)) {
    assert.ok(elapsed < 1200, "same-image theme transition must finish");
    await step(page, 16); elapsed += 16;
    await assertSurface(page);
    assert.ok(await page.evaluate(() => {
      const current = window.capture();
      return current.every((value, index) => value >= Math.min(window.themePixels.light[index], window.themePixels.dark[index]) - 1
        && value <= Math.max(window.themePixels.light[index], window.themePixels.dark[index]) + 1);
    }), "theme transitions must not flash outside their two endpoint colors");
  }
  assert.equal(await page.evaluate(() => window.results["theme-dark"]), true);
  assert.ok(await page.evaluate(() => window.maxDifference(window.themePixels.dark)) <= 1);

  // While a gesture is held, theme changes (including a resize notification)
  // must not repaint the canvas. Resume then rebuilds for the current theme.
  await page.evaluate(() => {
    window.ImageStudioViewerBackdrop.pause(window.owner); window.frozenTheme = window.capture();
    document.documentElement.dataset.theme = "light";
    window.ImageStudioViewerBackdrop.resize(window.owner);
  });
  await step(page, 1200);
  assert.equal(await page.evaluate(() => window.maxDifference(window.frozenTheme)), 0);
  assert.equal(await page.evaluate(() => window.owner.dataset.backdropState), "paused");
  await start(page, "theme-light-resume", await source(page, "gray"));
  await finish(page, "theme-light-resume"); await assertSurface(page);
  assert.ok(await page.evaluate(() => window.maxDifference(window.themePixels.light)) <= 1,
    "resuming the same image must publish the newly selected light theme");
}

async function verify(browser, name, reduced) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, reducedMotion: reduced ? "reduce" : "no-preference" });
  page.setDefaultTimeout(4000);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  try {
    await fixture(page);
    await verifyThemeBrightness(page);
    for (const color of ["opaque", "alpha"]) {
      await reset(page, color);
      await page.evaluate(() => { window.baseline = window.capture(); window.differences = []; });
      await start(page, "same", await source(page, color, "#same-pixels"));
      await scheduled(page);
      for (let index = 0; index < 21; index++) {
        await step(page, index ? 16 : 0);
        await page.evaluate(() => { window.differences.push(window.maxDifference(window.baseline)); });
        await assertSurface(page);
      }
      assert.equal(await page.evaluate(() => window.results.same), true);
      assert.ok(await page.evaluate(() => window.differences.every(difference => difference <= 1)),
        `${name} ${color}: identical content must not brighten or dim during a real canvas blend`);
    }

    await reset(page);
    await page.evaluate(() => { window.before = window.capture(); window.samples = []; });
    await start(page, "bright", await source(page, "light")); await scheduled(page); await step(page, 0);
    let duration = 0;
    while (await page.evaluate(() => window.results.bright === undefined)) {
      assert.ok(duration < 1200);
      await step(page, 16); duration += 16;
      await page.evaluate(() => { window.samples.push(window.capture()); });
    }
    const pixelChecks = await page.evaluate(() => {
      const after = window.capture();
      let withinEndpoints = true, intermediate = false, monotonic = true;
      for (let index = 0; index < after.length; index += 4) {
        for (let channel = 0; channel < 3; channel++) {
          const offset = index + channel, low = Math.min(window.before[offset], after[offset]), high = Math.max(window.before[offset], after[offset]);
          let previous = window.before[offset];
          for (const sample of window.samples) {
            const value = sample[offset];
            if (value < low - 1 || value > high + 1) withinEndpoints = false;
            if (value > low + 4 && value < high - 4) intermediate = true;
            if (value < previous - 1) monotonic = false;
            previous = value;
          }
        }
      }
      return { withinEndpoints, intermediate, monotonic };
    });
    assert.deepEqual(pixelChecks, { withinEndpoints: true, intermediate: true, monotonic: true });

    // Decode can finish during a held touch. It must not publish or rasterize
    // until the public transition call resumes the paused surface.
    await reset(page);
    const green = await source(page, "green");
    await page.evaluate(source => { window.arm(source); window.frozen = window.capture(); window.start("held", source); window.ImageStudioViewerBackdrop.pause(window.owner); }, green);
    await page.waitForFunction(source => window.gates.get(source)?.nativeReady, green, { polling: 10 });
    await page.evaluate(source => window.release(source), green);
    await page.waitForTimeout(30); await step(page, 1200);
    assert.deepEqual(await page.evaluate(() => ({ changed: window.maxDifference(window.frozen), rafs: window.rafs.size, phase: window.owner.dataset.backdropState })),
      { changed: 0, rafs: 0, phase: "paused" });
    await start(page, "resumed", green); await finish(page, "resumed");
    assert.equal(await page.evaluate(() => window.results.held), true);

    // Pausing an in-progress fade excludes idle wall time and a replacement
    // fade starts from the currently displayed pixels, not the old source.
    await reset(page);
    const light = await source(page, "light"), dark = await source(page, "dark");
    await start(page, "partial", light); await scheduled(page); await step(page, 0); await step(page, reduced ? 35 : 95);
    await page.evaluate(() => { window.frozen = window.capture(); window.ImageStudioViewerBackdrop.pause(window.owner); });
    await step(page, 1400);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0);
    await start(page, "continue", light); await step(page, 0);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0, "resume must not jump to the endpoint or rewind");
    await start(page, "reverse", dark); await scheduled(page); await step(page, 0);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0, "a new target must begin with the actual visible pixels");
    assert.equal(await page.evaluate(() => window.results.partial), false);
    await finish(page, "reverse");

    // Obsolete decoders and failed images cannot replace a successful surface.
    await reset(page);
    await page.evaluate(source => { window.arm(source); window.start("late", source); }, light);
    await page.waitForFunction(source => window.gates.get(source)?.nativeReady, light, { polling: 10 });
    await start(page, "newer", green); await finish(page, "newer");
    await page.evaluate(source => { window.frozen = window.capture(); window.release(source); }, light);
    await page.waitForTimeout(30); await step(page, 400);
    assert.equal(await page.evaluate(() => window.results.late), false);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0);
    await page.evaluate(source => { window.arm(source); window.start("failed", source); }, light);
    await page.waitForFunction(source => window.gates.get(source)?.nativeReady, light, { polling: 10 });
    await page.evaluate(source => window.release(source, true), light);
    await page.waitForFunction(() => window.results.failed !== undefined, null, { polling: 10 });
    assert.equal(await page.evaluate(() => window.results.failed), false);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0);

    // Rotation preserves one populated opaque node. A resize requested while
    // paused is deferred until resume, and subsequent transitions still work.
    await page.evaluate(() => window.ImageStudioViewerBackdrop.pause(window.owner));
    const originalSize = await page.evaluate(() => [window.owner.width, window.owner.height]);
    await page.setViewportSize({ width: 844, height: 390 });
    await page.evaluate(() => window.ImageStudioViewerBackdrop.resize(window.owner));
    assert.deepEqual(await page.evaluate(() => [window.owner.width, window.owner.height]), originalSize);
    await start(page, "resize-resume", green);
    assert.equal(await page.evaluate(() => window.results["resize-resume"]), true);
    assert.ok(await page.evaluate(() => window.owner.width > window.owner.height));
    await assertSurface(page);
    await start(page, "after-resize", dark); await finish(page, "after-resize");
    await assertSurface(page);

    await page.evaluate(source => { window.arm(source); window.start("disposed", source); }, light);
    await page.waitForFunction(source => window.gates.get(source)?.nativeReady, light, { polling: 10 });
    await page.evaluate(source => {
      window.frozen = window.capture(); window.ImageStudioViewerBackdrop.dispose(window.owner); window.release(source);
    }, light);
    await page.waitForTimeout(30); await step(page, 400);
    assert.equal(await page.evaluate(() => window.results.disposed), false);
    assert.equal(await page.evaluate(() => window.maxDifference(window.frozen)), 0);
    assert.equal(await page.evaluate(source => window.ImageStudioViewerBackdrop.transition(window.owner, source), green), false);
    assert.equal(await page.evaluate(() => window.rafs.size), 0);
    assert.deepEqual(errors, []);
    console.log(`${name} ${reduced ? "reduced" : "animated"}: theme/CSS brightness, canvas pixels, alpha coverage, pause/resume, supersession, stale decode, resize, bounded surface and disposal passed`);
    return duration;
  } finally { await page.close(); }
}

(async () => {
  for (const name of engines) {
    const browser = await playwright[name].launch({ headless: true });
    try {
      const normal = await verify(browser, name, false), reduced = await verify(browser, name, true);
      assert.ok(reduced < normal, `${name}: reduced motion must shorten the actual transition`);
    } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
