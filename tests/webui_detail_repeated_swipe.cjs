/* Gesture handover must accept the next touch before the previous animation ends.
 * This browser fixture does not connect to a server or mutate gallery data.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const root = path.resolve(__dirname, "../pages/image-studio");

async function install(page, cold = false, navigationDelay = 0) {
  await page.evaluate(({ cold, navigationDelay }) => {
    window.__repeatedSwipe?.dispose();
    const frame = document.querySelector(".detail-image-frame");
    frame.innerHTML = '<img class="detail-image" data-detail-image="0" alt="">';
    const items = Array.from({ length: 6 }, (_, index) => ({
      index,
      src: cold ? "" : `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="280" height="360"><rect width="280" height="360" fill="hsl(${index * 60} 40% 60%)"/></svg>`)}`,
    }));
    let cursor = 0;
    let stamp = performance.now();
    let releasePreviews;
    const previews = new Promise(resolve => { releasePreviews = resolve; });
    const calls = [], ends = [], errors = [];
    const render = () => {
      frame.dataset.cursor = String(cursor);
      const image = frame.querySelector("[data-detail-image]");
      image.dataset.detailImage = String(cursor);
      if (items[cursor].src) image.src = items[cursor].src;
      else image.removeAttribute("src");
    };
    const neighbor = direction => items[cursor + direction] || null;
    const dispose = ImageStudioDetailSwipe.bind(frame, {
      getNeighbor: neighbor,
      prepareNeighbor: direction => {
        const target = neighbor(direction);
        if (!target || !cold) return target;
        const pending = { ...target, previewReady: previews };
        previews.then(() => {
          pending.src = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>')}`;
        });
        return pending;
      },
      navigate: (direction, target) => {
        const commit = () => {
          if (target.index !== cursor + direction) throw new Error(`stale navigation from ${cursor} to ${target.index}`);
          cursor = target.index;
          calls.push(cursor);
          render();
          return true;
        };
        return navigationDelay ? new Promise(resolve => setTimeout(() => resolve(commit()), navigationDelay)) : commit();
      },
    });
    frame.addEventListener("detail-swipe-end", event => ends.push(event.detail));
    frame.addEventListener("detail-swipe-error", event => errors.push(String(event.detail.error)));
    function touch(type, dx = 0, fingers = 1, dy = 0) {
      const rect = frame.getBoundingClientRect();
      const point = { identifier: 41, target: frame, clientX: rect.x + rect.width / 2 + dx, clientY: rect.y + rect.height / 2 + dy };
      const touches = type === "touchend" || type === "touchcancel" ? [] : Array.from({ length: fingers }, (_, index) => ({ ...point, identifier: 41 + index }));
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: touches }, targetTouches: { value: touches }, changedTouches: { value: [point] }, timeStamp: { value: stamp += 25 },
      });
      frame.dispatchEvent(event);
    }
    const snapshot = () => ({
      cursor, calls: [...calls], errors: [...errors], ends: [...ends],
      phase: frame.dataset.detailSwipeState || "idle",
      overlays: frame.querySelectorAll(".detail-swipe-overlay").length,
      panes: Array.from(frame.querySelectorAll(".detail-swipe-pane img")).map(image => ({ src: image.getAttribute("src"), x: image.getBoundingClientRect().x })),
      illustrations: Array.from(frame.querySelectorAll(".detail-swipe-pane")).map(pane => {
        const marker = pane.querySelector(".image-studio-image-placeholder"), glyph = marker?.querySelector(".image-studio-placeholder-icon");
        const bounds = pane.getBoundingClientRect(), box = glyph?.getBoundingClientRect();
        return {
          offset: Number(pane.dataset.swipeOffset), count: pane.querySelectorAll(".image-studio-image-placeholder").length,
          visible: !!marker && getComputedStyle(marker).visibility !== "hidden" && Number(getComputedStyle(marker).opacity) > 0,
          paneX: bounds.x, x: box?.x, width: box?.width,
        };
      }),
    });
    const wait = delay => new Promise(resolve => setTimeout(resolve, delay));
    const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const first = async () => {
      touch("touchstart");
      touch("touchmove", -125);
      await frames();
      touch("touchend", -125);
      await wait(45);
      return snapshot();
    };
    window.__repeatedSwipe = { touch, snapshot, first, frames, wait, dispose, releasePreviews };
    render();
  }, { cold, navigationDelay });
}

async function settled(page, cursor, calls) {
  await page.waitForFunction(() => window.__repeatedSwipe.snapshot().phase === "idle", null, { timeout: 1600 });
  // Also wait beyond the abandoned animation's fallback timer: a late callback
  // must not commit an older target or remove a newer gesture's overlay.
  await page.waitForTimeout(330);
  const state = await page.evaluate(() => window.__repeatedSwipe.snapshot());
  assert.equal(state.cursor, cursor, JSON.stringify(state));
  assert.deepEqual(state.calls, calls, "each accepted swipe must commit exactly once");
  assert.equal(state.overlays, 0);
  assert.deepEqual(state.errors, []);
}

async function run(engine) {
  const browser = await engines[engine].launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  try {
    await page.setContent('<style>body{margin:0}.detail-image-frame{position:relative;width:360px;height:460px;overflow:hidden}.detail-image{width:100%;height:100%;object-fit:contain}</style><div class="detail-image-frame"></div>');
    await page.addStyleTag({ content: fs.readFileSync(path.join(root, "library.css"), "utf8") });
    await page.addStyleTag({ content: fs.readFileSync(path.join(root, "detail-swipe.css"), "utf8") });
    await page.addScriptTag({ content: fs.readFileSync(path.join(root, "vendor/lucide/icons.js"), "utf8") });
    await page.addScriptTag({ content: fs.readFileSync(path.join(root, "image-placeholder.js"), "utf8") });
    await page.addScriptTag({ content: fs.readFileSync(path.join(root, "detail-swipe.js"), "utf8") });

    for (const cold of [false, true]) {
      await install(page, cold);
      const repeated = await page.evaluate(async () => {
        const f = window.__repeatedSwipe;
        const before = await f.first();
        f.touch("touchstart");
        const caught = f.snapshot();
        f.touch("touchmove", -125);
        const following = f.snapshot();
        f.touch("touchend", -125);
        await f.wait(45);
        const beforeThird = f.snapshot();
        f.touch("touchstart");
        f.touch("touchmove", -125);
        const third = f.snapshot();
        f.touch("touchend", -125);
        return { before, caught, following, beforeThird, third };
      });
      assert.notEqual(repeated.before.phase, "idle", "fixture must begin the second touch before the first settles");
      assert.equal(repeated.following.phase, "dragging", `second touch was ignored: ${JSON.stringify(repeated)}`);
      assert.notEqual(repeated.beforeThird.phase, "idle");
      assert.equal(repeated.third.phase, "dragging", "third touch must also take over an unfinished animation");
      if (!cold) {
        const current = repeated.before.panes.find(pane => pane.x >= -1 && pane.x < 360);
        const caught = repeated.caught.panes.find(pane => pane.src === current?.src);
        assert.ok(current && caught && Math.abs(current.x - caught.x) < 3, `taking over must preserve the visible pane position: ${JSON.stringify(repeated)}`);
      } else {
        for (const [phase, state] of Object.entries(repeated)) {
          assert.equal(state.illustrations.length, state.panes.length, `${phase}: every cold pane should have an independent illustration`);
          for (const marker of state.illustrations) {
            assert.equal(marker.count, 1, `${phase}: repeated touch must neither duplicate nor discard pane illustrations`);
            assert.equal(marker.visible, true, `${phase}: the loading illustration should survive animation handover`);
            assert.ok(Math.abs(marker.width - 144) < 2 && Math.abs(marker.x + marker.width / 2 - marker.paneX - 180) < 2, `${phase}: illustration should remain centered in its moving pane: ${JSON.stringify(marker)}`);
          }
        }
        const current = repeated.before.illustrations.find(marker => marker.offset === 1);
        const caught = repeated.caught.illustrations.find(marker => marker.offset === 0);
        assert.ok(current && caught && Math.abs(current.x - caught.x) < 3, "taking over a cold transition must preserve the loading illustration's visible position");
      }
      await settled(page, 3, [1, 2, 3]);
      await page.evaluate(() => window.__repeatedSwipe.releasePreviews());
      await page.waitForTimeout(60);
      assert.equal((await page.evaluate(() => window.__repeatedSwipe.snapshot())).cursor, 3, "late preview work must not change the cursor");
    }

    await install(page);
    const reverse = await page.evaluate(async () => {
      const f = window.__repeatedSwipe;
      await f.first(); f.touch("touchstart"); f.touch("touchmove", 140);
      const state = f.snapshot(); f.touch("touchend", 140); return state;
    });
    assert.equal(reverse.phase, "dragging");
    await settled(page, 0, [1, 0]);

    for (const interruption of ["cancel", "multitouch", "vertical"]) {
      await install(page, true);
      await page.evaluate(async interruption => {
        const f = window.__repeatedSwipe;
        await f.first(); f.touch("touchstart");
        if (interruption === "vertical") {
          f.touch("touchmove", 2, 1, 100); f.touch("touchend", 2, 1, 100);
        } else {
          f.touch("touchmove", -125);
          f.touch(interruption === "cancel" ? "touchcancel" : "touchstart", -125, 2);
          f.touch("touchend", -125);
        }
      }, interruption);
      await settled(page, 1, [1]);
    }

    for (const cancelled of [false, true]) {
      await install(page, true, 120);
      const delayed = await page.evaluate(async cancelled => {
        const f = window.__repeatedSwipe;
        const pending = await f.first();
        f.touch("touchstart"); f.touch("touchmove", -125);
        f.touch(cancelled ? "touchcancel" : "touchend", -125);
        return pending;
      }, cancelled);
      assert.equal(delayed.cursor, 0, "second input must arrive while the first navigation metadata is pending");
      assert.notEqual(delayed.phase, "idle");
      await settled(page, cancelled ? 1 : 2, cancelled ? [1] : [1, 2]);
    }

    await install(page);
    await page.evaluate(async () => {
      const f = window.__repeatedSwipe;
      await f.first(); f.touch("touchstart"); f.touch("touchmove", -125);
      f.dispose(); f.touch("touchend", -125); f.releasePreviews();
    });
    await settled(page, 1, [1]);
    assert.deepEqual(pageErrors, []);
    console.log(`${engine}: repeated touches interrupt settling, preserve pane position, commit once, reverse, cancel, and close safely with loaded or pending media`);
  } finally { await page.close(); await browser.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_TEST_ENGINES || "chromium,webkit").split(",")) await run(engine);
})().catch(error => { console.error(error); process.exitCode = 1; });
