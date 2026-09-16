/* Pixel regression: a decoded backdrop crossfade must not expose the pale base. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const root = path.resolve(__dirname, "../../pages/image-studio");
const css = ["app.css", "library.css"].map(file => fs.readFileSync(path.join(root, file), "utf8")).join("\n");
const python = process.env.STUDIO_PYTHON || "python3";
const pixelReader = [
  "import json,sys",
  "from PIL import Image",
  'image=Image.open(sys.stdin.buffer).convert("RGB")',
  "print(json.dumps([image.getpixel((180,200)),image.getpixel((580,200))]))",
].join(";");
const pixels = buffer => JSON.parse(execFileSync(python, ["-c", pixelReader], { input: buffer, encoding: "utf8" }));
const painted = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const snapshot = async page => pixels(await page.screenshot({ animations: "allow" }));
const surfaces = ["detail", "viewer"];

async function verify(name, engine) {
  const browser = await engine.launch({ headless: true });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 400 } });
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent(`<style>${css}
      body { display:flex; gap:80px; padding:20px; background:white; }
      .detail-image-frame,.image-studio-pswp { position:relative; flex:none; width:320px; height:360px; min-height:0; border:0; border-radius:0; }
      .detail-image-frame { background:#e9efec; }
      .image-studio-pswp .pswp__bg { position:absolute; inset:0; }
    </style><div class="detail-image-frame"><div class="detail-image-background"><img id="detail" class="detail-image-backdrop"></div></div><div class="image-studio-pswp"><div class="pswp__bg"><div class="image-studio-viewer-background"><img id="viewer" class="image-studio-viewer-backdrop"></div></div></div>`);
    await page.addScriptTag({ path: path.join(root, "backdrop.js") });
    await page.evaluate(() => {
      const animate = Element.prototype.animate;
      window.fadeAnimations = [];
      Element.prototype.animate = function (...args) {
        const animation = animate.apply(this, args);
        animation.pause(); animation.currentTime = 0;
        window.fadeAnimations.push(animation);
        return animation;
      };
      window.solidImage = color => {
        const canvas = document.createElement("canvas"); canvas.width = canvas.height = 128;
        const context = canvas.getContext("2d"); context.fillStyle = color; context.fillRect(0, 0, 128, 128);
        return canvas.toDataURL();
      };
    });
    for (const scenario of [
      { label: "identical opaque", before: "#203040", after: "#203040", identical: true },
      { label: "identical alpha", before: "rgba(32,48,64,.5)", after: "rgba(32,48,64,.5)", identical: true },
      { label: "different colors", before: "#182838", after: "#d9b790", identical: false },
    ]) {
      const seeded = await page.evaluate(async ({ before, after }) => {
        window.fadeAnimations = [];
        const oldSource = window.solidImage(before) + "#old";
        window.targetSource = window.solidImage(after) + "#new";
        return Promise.all(["detail", "viewer"].map(async id => {
          const image = document.getElementById(id);
          window.ImageStudioBackdrop.dispose(image);
          image.src = oldSource;
          await image.decode();
          return window.ImageStudioBackdrop.seed(image, oldSource, { opacity: id === "detail" ? .62 : .64 });
        }));
      }, scenario);
      assert.deepEqual(seeded, [true, true]);
      assert.equal(await page.evaluate(() => window.fadeAnimations.length), 0, "seeding an already decoded preview never fades through the base");
      await painted(page);
      const before = await snapshot(page);
      await page.evaluate(() => {
        window.transitions = ["detail", "viewer"].map(id => window.ImageStudioBackdrop.transition(document.getElementById(id), window.targetSource, { opacity: id === "detail" ? .62 : .64 }));
      });
      await page.waitForFunction(() => window.fadeAnimations.length === (CSS.supports("mix-blend-mode", "plus-lighter") ? 4 : 2));
      assert.ok(await page.evaluate(() => [...document.images].every(image => image.complete && image.naturalWidth > 0)), "candidates are decoded before being mounted for the fade");
      // Freeze via the public gesture lifecycle so its running-time watchdog
      // is paused while screenshots deliberately hold the animation timeline.
      await page.evaluate(() => {
        for (const id of ["detail", "viewer"]) window.ImageStudioBackdrop.pause(document.getElementById(id));
      });
      const samples = [];
      for (const time of [0, 70, 140, 210, 279]) {
        await page.evaluate(time => window.fadeAnimations.forEach(animation => { animation.currentTime = time; }), time);
        await painted(page);
        samples.push(await snapshot(page));
      }
      await page.evaluate(() => {
        const ready = new Promise(resolve => { window.releaseCommitDecode = resolve; });
        for (const id of ["detail", "viewer"]) {
          const image = document.getElementById(id);
          image.decode = () => HTMLImageElement.prototype.decode.call(image).then(() => ready);
        }
        window.fadeAnimations.forEach(animation => animation.finish());
        window.transitions = ["detail", "viewer"].map(id => window.ImageStudioBackdrop.transition(document.getElementById(id), window.targetSource, { opacity: id === "detail" ? .62 : .64 }));
      });
      await page.waitForFunction(() => ["detail", "viewer"].every(id => document.getElementById(id).getAttribute("src") === window.targetSource));
      assert.equal(await page.locator("img").count(), 4, "decoded candidates keep covering the image while its stable node decodes the new src");
      await painted(page);
      samples.push(await snapshot(page));
      await page.evaluate(async () => {
        for (const id of ["detail", "viewer"]) window.ImageStudioBackdrop.pause(document.getElementById(id));
        window.releaseCommitDecode();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      assert.equal(await page.locator("img").count(), 4, "a gesture must retain the painted cover through the commit handoff");
      const results = await page.evaluate(async () => {
        for (const id of ["detail", "viewer"]) delete document.getElementById(id).decode;
        window.resumedTransitions = ["detail", "viewer"].map(id => window.ImageStudioBackdrop.transition(document.getElementById(id), window.targetSource, { opacity: id === "detail" ? .62 : .64 }));
        if (window.fadeAnimations.some(animation => animation.currentTime < 280)) throw new Error("resuming a completed fade rewound its animation");
        return Promise.all(window.resumedTransitions);
      });
      assert.deepEqual(results, [true, true]);
      await painted(page);
      const after = await snapshot(page);
      assert.equal(await page.locator("img").count(), 2, "candidate layers are removed after committing the new resource");
      for (let surface = 0; surface < surfaces.length; surface++) {
        for (let channel = 0; channel < 3; channel++) {
          const first = before[surface][channel], last = after[surface][channel];
          const values = samples.map(sample => sample[surface][channel]);
          if (scenario.identical) {
            const all = [first, ...values, last];
            assert.ok(Math.max(...all) - Math.min(...all) <= 3, `${name} ${surfaces[surface]} ${scenario.label}: channel ${channel} flashes: ${all}`);
          } else {
            const low = Math.min(first, last), high = Math.max(first, last);
            assert.ok(values.every(value => value >= low - 3 && value <= high + 3), `${name} ${surfaces[surface]}: fade exceeds endpoint brightness: ${[first, ...values, last]}`);
            assert.ok(values[2] > low + 8 && values[2] < high - 8, "the midpoint blends the two images rather than switching abruptly");
            assert.ok(values.every((value, index) => !index || value >= values[index - 1] - 3), "the crossfade progresses without a backwards brightness jump");
          }
        }
      }
      console.log(`${name}: ${scenario.label}, detail + fullscreen pixel coverage and decode handoff passed`);
    }
    // The first decoded layer also needs a covered handoff: without an old
    // fade animation, displaying both RGBA images would briefly double alpha.
    await page.evaluate(() => {
      const ready = new Promise(resolve => { window.releaseInitialDecode = resolve; });
      window.targetSource = window.solidImage("rgba(32,48,64,.5)") + "#initial";
      window.initialTransitions = ["detail", "viewer"].map(id => {
        const image = document.getElementById(id);
        window.ImageStudioBackdrop.dispose(image); image.removeAttribute("src");
        image.decode = () => HTMLImageElement.prototype.decode.call(image).then(() => ready);
        return window.ImageStudioBackdrop.transition(image, window.targetSource, { opacity: id === "detail" ? .62 : .64 });
      });
    });
    await page.waitForFunction(() => ["detail", "viewer"].every(id => document.getElementById(id).src === window.targetSource && document.getElementById(id).complete));
    await painted(page);
    const heldInitial = await snapshot(page);
    await page.evaluate(async () => {
      window.releaseInitialDecode(); await Promise.all(window.initialTransitions);
      for (const id of ["detail", "viewer"]) delete document.getElementById(id).decode;
    });
    await painted(page);
    const finalInitial = await snapshot(page);
    assert.ok(heldInitial.every((pixel, index) => pixel.every((channel, offset) => Math.abs(channel - finalInitial[index][offset]) <= 3)), "initial alpha background must not be drawn twice during handoff");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}

(async () => { await verify("Chromium", chromium); await verify("WebKit", webkit); })().catch(error => { console.error(error); process.exitCode = 1; });
