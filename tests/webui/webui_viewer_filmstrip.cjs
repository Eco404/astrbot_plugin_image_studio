/* Group-scoped lightbox filmstrip; run only against the isolated harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const engines = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-viewer-filmstrip-"));
const marker = path.basename(output);
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];paths=[]
for index in range(44):
 image=Image.new("RGB",(360,480),(90+index*3,190-index*2,180));draw=ImageDraw.Draw(image)
 draw.polygon([(0,350),(150,100+index),(360,350)],fill=(150,110+index*2,160));draw.rectangle((0,350,360,480),fill=(80,150,120))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("Software","NovelAI");metadata.add_text("Comment",json.dumps({"prompt":f"{marker} landscape {index}","model":"filmstrip-fixture","steps":23,"seed":index,"request_type":"PromptGenerateRequest"}))
 file=folder/f"{index}.png";image.save(file,pnginfo=metadata);paths.append(str(file))
print(json.dumps(paths))
`;
function gate() { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; }
async function api(client, method, endpoint, data) {
  const response = await client[method](`${apiRoot}/${endpoint}`, data ? { data } : {});
  assert.ok(response.ok(), `${endpoint}: ${response.status()}`);
  const body = await response.json(); return body.data || body;
}
async function seed(client, files, suffix) {
  const items = files.map((file, index) => ({ client_id: `${marker}_${suffix}_${index}`, filename: path.basename(file), sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"), overrides: { model: "filmstrip-fixture" } }));
  const batch = await api(client, "post", "imports/prepare", { items, as_group: files.length > 1 }); assert.equal(batch.allowed, true);
  for (let index = 0; index < files.length; index++) {
    const response = await client.post(`${apiRoot}/${batch.items[index].upload_endpoint}`, { multipart: { file: { name: path.basename(files[index]), mimeType: "image/png", buffer: fs.readFileSync(files[index]) } } });
    assert.ok(response.ok());
  }
  return (await api(client, "post", batch.commit_endpoint, {})).generation_ids[0];
}
async function frames(frame, count = 6) {
  await frame.evaluate(remaining => new Promise(resolve => { const tick = () => --remaining <= 0 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick); }), count);
}
async function tapImage(page, frame) {
  const point = await frame.evaluate(() => {
    const box = window.__filmViewer.currSlide.content.element.getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height * .3 };
  });
  await page.mouse.click(point.x, point.y);
}
async function ready(frame) {
  await frame.waitForFunction(() => window.__filmViewer?.opener.isOpen && window.__filmViewer.currSlide.content.element?.naturalWidth > 1);
}

async function run(browser, engine, viewport, groups, manifests) {
  const page = await browser.newPage({ viewport, hasTouch: true });
  page.setDefaultTimeout(15000);
  const errors = [], requests = []; page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.includes("/gallery/image/")) requests.push({ id: url.pathname.split("/").at(-1), detail: url.searchParams.get("detail") });
  });
  const far = manifests.large.images.at(-1).id;
  const delayedId = manifests.large.images[7].id;
  const pending = gate(), delayedPreview = gate();
  await page.route(`**/gallery/image/${far}?*`, async route => { await pending.promise; await route.continue(); });
  await page.route(`**/gallery/image/${delayedId}?*`, async route => {
    if (new URL(route.request().url()).searchParams.get("detail") === "preview") await delayedPreview.promise;
    await route.continue();
  });
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#modelChoice:not(:disabled)").waitFor();
    await frame.evaluate(async dark => {
      await window.ImageStudioAppearance.ready; window.ImageStudioAppearance.set({ preference: dark ? "dark" : "light" });
      const Original = window.PhotoSwipe;
      window.PhotoSwipe = class extends Original { constructor(options) { super(options); window.__filmViewer = this; } };
      window.AstrBotPluginPage.download = async (endpoint, parameters, filename) => { window.__filmDownload = { endpoint, filename }; };
    }, engine === "webkit");
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker); await frame.locator("#gallerySearch").press("Tab");
    await frame.locator(`[data-gallery-id="${groups.large}"] .gallery-info`).click();
    await frame.locator("#detailCopy:not(:disabled)").waitFor();
    await frame.locator("[data-detail-image]").click(); await ready(frame);
    const strip = frame.locator(".image-studio-viewer-filmstrip");
    const download = frame.locator(".pswp__button--image-studio-download");
    assert.equal(await strip.isVisible(), false); assert.equal(await download.isVisible(), false);
    await tapImage(page, frame);
    await strip.waitFor(); await download.waitFor();
    await frame.waitForFunction(() => document.querySelector('.image-studio-viewer-filmstrip [aria-current="true"] img')?.naturalWidth > 0);
    assert.equal(await strip.getAttribute("data-generation-id"), groups.large);
    assert.equal(await strip.locator("[data-viewer-index]").count(), 40);
    const startIndex = await frame.evaluate(() => window.__filmViewer.currIndex);
    const hiddenImage = manifests.large.images[25].id;
    assert.equal(requests.filter(item => item.id === hiddenImage).length, 0, "showing controls must not fetch the entire group");
    await frames(frame, 12);
    const barBox = await strip.boundingBox(), buttonBox = await download.boundingBox();
    assert.ok(buttonBox.y + buttonBox.height <= barBox.y, "download belongs above the filmstrip");
    assert.ok(Math.abs(buttonBox.x + buttonBox.width - barBox.x - barBox.width) < 2, "download aligns with the right edge of the filmstrip on tablets too");
    assert.ok(barBox.x >= 0 && barBox.x + barBox.width <= viewport.width + 1);
    const originalScroll = await strip.evaluate(element => element.scrollLeft);
    if (engine === "chromium") {
      const cdp = await page.context().newCDPSession(page);
      try {
        const y = barBox.y + barBox.height / 2, from = barBox.x + barBox.width * .85, to = barBox.x + barBox.width * .2;
        await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from, y, id: 1 }] });
        for (let index = 1; index <= 8; index++) {
          await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: from + (to - from) * index / 8, y, id: 1 }] }); await frames(frame, 1);
        }
        await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      } finally { await cdp.detach(); }
    } else { await page.mouse.move(barBox.x + barBox.width / 2, barBox.y + barBox.height / 2); await page.mouse.wheel(480, 0); }
    await frame.waitForFunction(previous => document.querySelector(".image-studio-viewer-filmstrip").scrollLeft > previous + 10, originalScroll);
    assert.equal(await frame.evaluate(() => window.__filmViewer.currIndex), startIndex, "scrolling the strip must not page the main image");
    assert.equal(await strip.isVisible(), true);
    await strip.evaluate(element => { element.scrollLeft = element.scrollWidth; });
    const last = strip.locator("[data-viewer-index]").last();
    await last.click();
    await frame.waitForFunction(index => window.__filmViewer.currIndex === index + 39, startIndex);
    await last.locator('[class~="detail-filmstrip-preview"]').waitFor();
    await frame.waitForFunction(() => Number(document.querySelector('.image-studio-viewer-filmstrip [aria-current="true"]').dataset.viewerIndex) === window.__filmViewer.currIndex);
    assert.equal(await strip.isVisible(), true, "choosing a thumbnail must keep controls open");
    await download.click();
    assert.equal(await frame.evaluate(() => window.__filmDownload.endpoint), `gallery/download/${far}`);
    pending.release(); await ready(frame);
    const farReads = requests.filter(item => item.id === far && item.detail === "preview").length;
    assert.equal(farReads, 1, "filmstrip and viewer must share the pending preview");

    await tapImage(page, frame); await strip.waitFor({ state: "hidden" }); assert.equal(await download.isVisible(), false);
    await tapImage(page, frame); await strip.waitFor();
    assert.equal(requests.filter(item => item.id === far && item.detail === "preview").length, farReads, "toggling controls must reuse previews");
    await page.screenshot({ path: path.join(output, `${engine}-${viewport.width}-group.png`) });

    // Hold the main viewer's idle gate: switching groups must not hide and
    // recreate the glass surface while the new thumbnail content waits.
    const beforeHandoff = await strip.boundingBox();
    const duringHandoff = await frame.evaluate(id => {
      const viewer = window.__filmViewer;
      const original = document.querySelector(".image-studio-viewer-filmstrip");
      viewer.dispatch("pointerDown", { originalEvent: { pointerId: 93, pointerType: "touch", isPrimary: true } });
      viewer.goTo(viewer.options.dataSource.findIndex(item => item.generation_id === id));
      const strip = document.querySelector(".image-studio-viewer-filmstrip");
      const index = viewer.currIndex;
      strip.querySelector("[data-viewer-index]").click();
      return { same: strip === original, hidden: strip.hidden, inert: strip.inert, pending: strip.hasAttribute("data-group-pending"), stayedOnTarget: index === viewer.currIndex };
    }, groups.small);
    assert.deepEqual(duringHandoff, { same: true, hidden: false, inert: true, pending: true, stayedOnTarget: true });
    await frames(frame, 12);
    assert.equal(await strip.isVisible(), true, "glass must stay visible even while content rendering is delayed");
    const delayedHandoff = await strip.evaluate(element => ({ opacity: getComputedStyle(element).opacity, content: getComputedStyle(element.firstElementChild).visibility, busy: element.getAttribute("aria-busy") }));
    assert.deepEqual(delayedHandoff, { opacity: "1", content: "hidden", busy: "true" });
    const afterHandoff = await strip.boundingBox();
    for (const key of ["x", "y", "width", "height"]) assert.ok(Math.abs(beforeHandoff[key] - afterHandoff[key]) < 1, `glass ${key} changed during handoff`);
    await frame.evaluate(() => window.__filmViewer.dispatch("pointerUp", { originalEvent: { pointerId: 93, pointerType: "touch", type: "pointerup" } }));
    await frame.waitForFunction(id => {
      const strip = document.querySelector(".image-studio-viewer-filmstrip");
      return strip.dataset.generationId === id && strip.getAttribute("aria-busy") === "false" && !strip.inert;
    }, groups.small);

    for (const name of ["small", "single", "large"]) {
      await frame.evaluate(id => window.__filmViewer.goTo(window.__filmViewer.options.dataSource.findIndex(item => item.generation_id === id)), groups[name]);
      await ready(frame);
      if (name === "single") {
        await strip.waitFor({ state: "hidden" }); assert.equal(await download.isVisible(), true);
        const box = await download.boundingBox(); assert.ok(box.y + box.height > viewport.height - 40, "single-image downloads return to the bottom");
      } else {
        await frame.waitForFunction(id => document.querySelector(".image-studio-viewer-filmstrip:not([hidden])")?.dataset.generationId === id, groups[name]);
        assert.equal(await strip.locator("[data-viewer-index]").count(), name === "small" ? 3 : 40);
        assert.equal(await strip.locator('[aria-current="true"]').count(), 1);
      }
    }
    // Returning via a single-image group reuses the large group's DOM. An old
    // pending response must still fill its thumbnail under the new visibility epoch.
    await frame.evaluate(id => window.__filmViewer.goTo(window.__filmViewer.options.dataSource.findIndex(item => item.generation_id === id)), groups.single);
    await strip.waitFor({ state: "hidden" });
    await frame.evaluate(id => window.__filmViewer.goTo(window.__filmViewer.options.dataSource.findIndex(item => item.generation_id === id)), groups.large);
    await frame.waitForFunction(id => document.querySelector(".image-studio-viewer-filmstrip:not([hidden])")?.dataset.generationId === id, groups.large);
    delayedPreview.release();
    await frame.waitForFunction(index => document.querySelector(`[data-viewer-index="${index}"] img`)?.naturalWidth > 0, startIndex + 7);
    assert.equal(requests.filter(item => item.id === delayedId && item.detail === "preview").length, 1);
    await frame.evaluate(() => window.__filmViewer.close()); await frame.locator(".pswp--open").waitFor({ state: "detached" });
    assert.equal(await strip.count(), 0);
    assert.deepEqual(errors, []);
    console.log(`${engine}-${viewport.width}: toggle, group-only thumbnails, native strip scrolling, pending/cache reuse, selection, download positioning/identity and cleanup passed`);
  } finally { pending.release(); delayedPreview.release(); await page.close(); }
}

(async () => {
  const client = await engines.request.newContext();
  const groups = {};
  try {
    const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
    groups.large = await seed(client, files.slice(0, 40), "large");
    groups.small = await seed(client, files.slice(40, 43), "small");
    groups.single = await seed(client, files.slice(43), "single");
    const manifests = Object.fromEntries(await Promise.all(Object.entries(groups).map(async ([name, id]) => [name, await api(client, "get", `gallery/detail/${id}?light=1`)])));
    for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
      const browser = await engines[engine].launch({ headless: true });
      try { for (const viewport of [{ width: 390, height: 844 }, { width: 1366, height: 1024 }]) await run(browser, engine, viewport, groups, manifests); }
      finally { await browser.close(); }
    }
    console.log(`Screenshots: ${output}`);
  } finally { if (Object.keys(groups).length) await api(client, "post", "gallery/delete", { ids: Object.values(groups) }); await client.dispose(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
