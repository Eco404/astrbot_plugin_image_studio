/* Sample the real PhotoSwipe tree after a swipe, with originals already loaded. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const engine = process.env.STUDIO_BROWSER || "chromium";
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-viewer-compositor-"));
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";

async function install(inner) {
  await inner.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 900; canvas.height = 1200;
    const context = canvas.getContext("2d"); context.fillStyle = "#203040"; context.fillRect(0, 0, 900, 1200);
    const source = canvas.toDataURL();
    const Native = window.PhotoSwipe;
    window.PhotoSwipe = class extends Native {
      constructor(options) {
        // Equal dark pixels with distinct immutable URLs isolate a flash from
        // an actual brightness change. Both resolutions are already present.
        options.dataSource.forEach((item, index) => Object.assign(item, {
          src: `${source}#${index}`, msrc: `${source}#${index}`,
          previewSrc: `${source}#${index}`, originalSrc: `${source}#${index}`,
          width: 900, height: 1200, loadedDetail: "original",
        }));
        super(options); window.__compositorViewer = this;
      }
    };
    window.__compositorPointer = (type, x) => {
      window.__compositorViewer.scrollWrap.dispatchEvent(new PointerEvent(type, {
        pointerId: 91, pointerType: "touch", isPrimary: true,
        bubbles: true, cancelable: true, clientX: x, clientY: 410,
        buttons: /up|cancel/.test(type) ? 0 : 1, button: 0,
      }));
    };
  });
}

async function settled(inner) {
  await inner.waitForFunction(() => {
    const viewer = window.__compositorViewer;
    const backdrop = viewer?.bg.querySelector("canvas");
    return viewer?.opener.isOpen && !viewer.mainScroll.isShifted()
      && !viewer.animations.activeAnimations.length
      && backdrop?.dataset.backdropState === "idle"
      && backdrop.dataset.previewSource === viewer.currSlide.data.previewSrc;
  });
}

async function swipe(inner, direction) {
  await inner.evaluate(async direction => {
    const start = direction > 0 ? 320 : 70;
    window.__compositorPointer("pointerdown", start);
    for (let step = 1; step <= 6; step++) {
      window.__compositorPointer("pointermove", start - direction * 40 * step);
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
    }
    window.__compositorPointer("pointerup", start - direction * 240);
  }, direction);
}

async function frames(inner, label) {
  const records = [];
  const started = Date.now();
  // Continue past PhotoSwipe's delayed placeholder removal, not just through
  // the 280ms background blend. Screenshots inspect actual composed pixels.
  while (Date.now() - started < 1500 || records.length < 8) {
    const state = await inner.evaluate(() => {
      const viewer = window.__compositorViewer, backdrop = viewer.bg.querySelector("canvas");
      return {
        index: viewer.currIndex, shifted: viewer.mainScroll.isShifted(), opacity: viewer.bgOpacity,
        phase: backdrop.dataset.backdropState, source: viewer.currSlide.content.element?.getAttribute("src"),
        expected: viewer.currSlide.data.originalSrc,
        canvasStable: backdrop === window.__compositorCanvas,
        placeholders: viewer.currSlide.container.querySelectorAll(".pswp__img--placeholder").length,
      };
    });
    const file = path.join(output, `${label}-${records.length}.png`);
    await inner.locator(".pswp").screenshot({ path: file });
    records.push({ ...state, file });
  }
  return records;
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const theme of ["light", "dark"]) {
      const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, hasTouch: true });
      page.setDefaultTimeout(15000);
      const errors = []; page.on("pageerror", error => errors.push(error.message));
      await page.goto(base);
      const inner = page.frames().find(frame => frame.url().includes("/ui/"));
      await inner.locator("#modelChoice:not(:disabled)").waitFor();
      await inner.evaluate(theme => window.ImageStudioAppearance.set({ preference: theme }), theme);
      await install(inner);
      await inner.locator('[data-view="gallery"]').click();
      await inner.locator(".gallery-card .gallery-info").first().click();
      await inner.locator("[data-detail-image]").evaluate(image => image.click());
      await settled(inner);
      await inner.evaluate(() => { window.__compositorCanvas = window.__compositorViewer.bg.querySelector("canvas"); });
      const baseline = (await frames(inner, `${theme}-baseline`)).at(-1);
      const samples = [];
      for (const direction of [1, -1]) {
        await swipe(inner, direction);
        samples.push(...await frames(inner, `${theme}-${direction}`));
        await settled(inner);
      }
      const pixels = JSON.parse(execFileSync(python, ["-c", String.raw`
import json,sys
from PIL import Image
result=[]
for name in json.load(sys.stdin):
 with Image.open(name) as image:
  image=image.convert("RGB");w,h=image.size
  result.append([list(image.getpixel((round(w*.08),round(h*.06)))),list(image.getpixel((w//2,h//2)))])
print(json.dumps(result))
`], { input: JSON.stringify([baseline.file, ...samples.map(frame => frame.file)]), encoding: "utf8" }));
      assert.ok(samples.length >= 12, "inspect multiple composed frames through and after settling");
      for (let index = 0; index < samples.length; index++) {
        const sample = samples[index];
        assert.equal(sample.opacity, 1); assert.equal(sample.canvasStable, true);
        for (let channel = 0; channel < 3; channel++) {
          assert.ok(Math.abs(pixels[index + 1][0][channel] - pixels[0][0][channel]) <= 4,
            `${theme}: backdrop flashed in ${sample.file}: ${pixels[index + 1][0]} vs ${pixels[0][0]}`);
          if (!sample.shifted) assert.ok(Math.abs(pixels[index + 1][1][channel] - pixels[0][1][channel]) <= 4,
            `${theme}: stable original flashed after settling in ${sample.file}`);
        }
        assert.equal(sample.source, sample.expected, "sample the already-loaded original, not a resolution upgrade");
      }
      // A system/appearance change re-rasterizes the same thumbnail after the
      // held touch ends. It must not paint during that touch or replace nodes.
      await inner.evaluate(theme => {
        const canvas = window.__compositorCanvas;
        window.__themePixel = Array.from(canvas.getContext("2d").getImageData(0, 0, 1, 1).data);
        window.__compositorPointer("pointerdown", 195);
        window.ImageStudioAppearance.set({ preference: theme === "light" ? "dark" : "light" });
      }, theme);
      await page.waitForTimeout(200);
      assert.equal(await inner.evaluate(() => {
        const pixel = window.__compositorCanvas.getContext("2d").getImageData(0, 0, 1, 1).data;
        return pixel.every((value, index) => value === window.__themePixel[index]);
      }), true, "the theme observer must defer rendering while a finger is held");
      await inner.evaluate(() => window.__compositorPointer("pointercancel", 195));
      await inner.waitForFunction(() => {
        const canvas = window.__compositorCanvas, pixel = canvas.getContext("2d").getImageData(0, 0, 1, 1).data;
        return canvas.dataset.backdropState === "idle" && pixel[3] === 255
          && Math.abs(pixel[0] - window.__themePixel[0]) > 40;
      });
      assert.equal(await inner.evaluate(() => window.__compositorViewer.bg.querySelector("canvas") === window.__compositorCanvas), true);
      assert.deepEqual(errors, []);
      console.log(`${engine} ${theme}: ${samples.length} composed frames, DPR 3, real PhotoSwipe gestures, stable original, opaque background and deferred theme update passed`);
      await page.close();
    }
  } finally { await browser.close(); }
  console.log(`Compositor screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
