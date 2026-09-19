/* Inline group titles use explicit confirmation; cancellation never writes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-gallery-titles-"));
const prefix = `${base}/astrbot_plugin_image_studio/`;

async function verify(engine, name, width) {
  const browser = await engine.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 960 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], writes = [];
  page.on("pageerror", error => errors.push(error.message));
  let failSave = false;
  await page.route("**/gallery/title", async route => {
    writes.push(route.request().postDataJSON());
    if (failSave) return route.fulfill({ status: 503, json: { message: "模拟标题保存失败" } });
    await route.continue();
  });
  let frame, identity, fallback;
  const card = () => frame.locator(`[data-gallery-id="${identity}"]`);
  const input = () => card().locator('input[aria-label="图组标题"]');
  const title = () => card().locator("[data-gallery-title]");
  async function loaded() {
    await page.locator("#studio").waitFor();
    frame = await (await page.locator("#studio").elementHandle()).contentFrame();
    await frame.waitForURL(/\/ui\//);
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
  }
  const read = async () => (await (await page.request.get(prefix+`gallery/detail/${identity}?light=1`)).json()).title;
  async function confirm() {
    const response = page.waitForResponse(response => response.url().endsWith("/gallery/title") && response.request().method() === "POST");
    await card().locator('[aria-label="保存图组标题"]').click();
    await response;
  }
  try {
    await page.goto(base); await loaded();
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#galleryGrid [data-gallery-id]").first().waitFor();
    identity = await frame.locator("#galleryGrid [data-gallery-id]").first().getAttribute("data-gallery-id");
    await page.request.post(prefix+"gallery/title", { data: { generation_id: identity, title: "" } });
    await frame.locator("#galleryRefresh").click();
    await title().waitFor();
    fallback = await title().textContent();
    assert.ok(fallback);
    await title().click();
    assert.equal(await input().inputValue(), "", "the displayed model fallback must not prefill the saved title");
    assert.equal(await frame.locator("#detailDrawer").evaluate(node => node.classList.contains("is-open")), false);
    await input().fill("cancelled title");
    await frame.locator("#gallerySearch").click();
    await title().waitFor();
    assert.equal(await title().textContent(), fallback);
    assert.equal(await read(), "");
    assert.equal(writes.length, 0);

    await title().click();
    await input().fill("escape title");
    await input().press("Escape");
    assert.equal(writes.length, 0);
    await title().press("Enter");
    await input().fill("初夏猫咪 <b>图组</b> 😀");
    await page.screenshot({ path: path.join(output, `${name}-${width}-editing.png`) });
    await confirm();
    await title().waitFor();
    assert.equal(await title().textContent(), "初夏猫咪 <b>图组</b> 😀");
    assert.equal(await title().locator("b").count(), 0, "titles must render as plain text");
    assert.equal(await read(), "初夏猫咪 <b>图组</b> 😀");
    assert.equal(writes.length, 1);
    assert.equal(await frame.locator("#detailDrawer").evaluate(node => node.classList.contains("is-open")), false);
    await title().click();
    assert.equal(await input().inputValue(), "初夏猫咪 <b>图组</b> 😀");
    await input().fill("failed edit");
    failSave = true; await confirm(); failSave = false;
    await frame.locator("#appNotice").filter({ hasText: "模拟标题保存失败" }).waitFor();
    assert.equal(await read(), "初夏猫咪 <b>图组</b> 😀");
    await input().press("Escape");

    await frame.locator("#gallerySearch").fill("初夏猫咪");
    await frame.locator("#gallerySearch").press("Enter");
    await title().waitFor();
    const results = await (await page.request.get(prefix+"gallery/list?light=1&query="+encodeURIComponent("初夏猫咪"))).json();
    assert.ok(results.items.some(item => item.id === identity));
    await page.reload(); await loaded();
    await frame.locator('[data-view="gallery"]').click();
    await title().waitFor();
    assert.equal(await title().textContent(), "初夏猫咪 <b>图组</b> 😀");
    await title().click();
    await input().fill(""); await confirm(); await title().waitFor();
    assert.equal(await title().textContent(), fallback);
    assert.equal(await read(), "");
    await card().locator(".gallery-image-wrap").click();
    await frame.locator("#detailDrawer.is-open").waitFor();
    assert.deepEqual(errors, []);
    console.log(`${name} ${width}: default, confirm, outside/Escape cancellation, retry, reload, clear, search and image clicks passed`);
  } finally { await browser.close(); }
}
(async () => {
  for (const [name, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    for (const width of [390, 1440]) await verify(engine, name, width);
  }
  console.log(`Gallery title screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
