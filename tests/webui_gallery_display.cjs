/* Run against an isolated webui_harness.py, never deployment data. */
const assert = require("node:assert/strict");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function verify(browser, name, width) {
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const requests = [];
  page.on("request", request => { if (request.url().includes("/gallery/list?")) requests.push(new URL(request.url())); });
  let frame;
  const open = async () => {
    await page.goto(base);
    frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator(".gallery-card").first().waitFor();
    await settle(frame);
  };
  const refresh = async () => {
    const response = page.waitForResponse(response => response.url().includes("/gallery/list?"));
    await frame.locator("#galleryRefresh").click();
    const payload = await (await response).json(); await settle(frame); return payload;
  };
  const values = id => frame.locator(`#${id}`).evaluate(select => [...select.options].map(option => option.value));
  const selected = id => frame.locator(`#${id}`).evaluate(select => [...select.selectedOptions].map(option => option.value));
  const trigger = id => frame.locator(`.studio-select-trigger[data-select-id="${id}"]`);
  try {
    await open();
    const payload = await refresh();
    assert.deepEqual(await values("galleryProvider"), payload.filters.providers.map(item => item.id));
    assert.deepEqual(await values("galleryMode"), ["text2img"], "empty img2img/unknown categories must not appear");
    assert.deepEqual(new Set(await values("gallerySource")), new Set(["webui", "command", "llm_tool"]));
    assert.ok(!(await values("galleryProvider")).includes(""), "unassigned provider requires actual records");

    if (width < 600) {
      const layout = await frame.evaluate(() => {
        const box = selector => { const { x, y, width, height, right, bottom } = document.querySelector(selector).getBoundingClientRect(); return { x, y, width, height, right, bottom }; };
        return { search: box("#gallerySearch"), favorite: box("#galleryFavorite"), refresh: box("#galleryRefresh"), filters: ["galleryProvider", "galleryMode", "gallerySource", "galleryEngine"].map(id => box(`[data-select-id="${id}"]`)) };
      });
      assert.ok(Math.abs(layout.search.bottom - layout.favorite.bottom) < 1 && Math.abs(layout.search.bottom - layout.refresh.bottom) < 1, JSON.stringify(layout));
      assert.ok(layout.search.right + 6 <= layout.favorite.x && layout.favorite.right + 6 <= layout.refresh.x, JSON.stringify(layout));
      assert.ok(Math.abs(layout.filters[0].y - layout.filters[1].y) < 1 && Math.abs(layout.filters[2].y - layout.filters[3].y) < 1);
      assert.ok(Math.abs(layout.filters[0].width - layout.filters[1].width) < 1);
    }

    const label = frame.locator("#galleryPageLabel"), picker = frame.locator("#galleryPagePicker");
    const footerBefore = await frame.locator(".gallery-floatingbar").boundingBox();
    await label.click(); assert.equal(await picker.isVisible(), true);
    const pickerBox = await picker.boundingBox(), footerAfter = await frame.locator(".gallery-floatingbar").boundingBox();
    assert.ok(pickerBox.y + pickerBox.height < footerAfter.y);
    assert.ok(Math.abs(footerAfter.height - footerBefore.height) < 1 && Math.abs(footerAfter.y - footerBefore.y) < 1);
    assert.equal(await frame.locator('[data-gallery-page="0"]').getAttribute("aria-current"), "page");
    await frame.locator('[data-gallery-page="1"]').click();
    await label.filter({ hasText: "第 2" }).waitFor(); assert.equal(await picker.isVisible(), false);
    await label.click(); await label.click(); assert.equal(await picker.isVisible(), false);
    await label.click(); await label.press("Escape"); assert.equal(await picker.isVisible(), false);
    await label.click(); await frame.locator("#gallerySearch").click(); assert.equal(await picker.isVisible(), false);
    await label.click(); await frame.evaluate(() => document.dispatchEvent(new Event("scroll"))); assert.equal(await picker.isVisible(), false);
    if (width < 600) {
      await label.click();
      await frame.evaluate(() => {
        const view = document.getElementById("galleryView");
        const send = (type, y) => { const event = new Event(type, { bubbles: true }); Object.defineProperty(event, "touches", { value: [{ clientX: 100, clientY: y }] }); view.dispatchEvent(event); };
        send("touchstart", 120); send("touchmove", 150);
      });
      assert.equal(await picker.isVisible(), false);
    }

    // Windowing keeps all pages reachable without one DOM element per page.
    await page.route("**/gallery/list?*", async route => {
      const response = await route.fetch(), body = await response.json(); body.total = body.limit * 10000;
      await route.fulfill({ response, json: body });
    });
    await refresh(); await label.click();
    await frame.locator("#galleryPageCards").evaluate(strip => { strip.scrollLeft = strip.scrollWidth; });
    await frame.locator('[data-gallery-page="9999"]').waitFor();
    assert.ok(await frame.locator(".gallery-page-card").count() < 50);
    assert.equal(await picker.isVisible(), true, "horizontal page scrolling must keep the picker open");
    await label.click(); await page.unroute("**/gallery/list?*"); await refresh();

    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    assert.deepEqual(await frame.locator(".settings-layout > .settings-panel h2").allTextContents(), ["运行", "默认值", "历史", "图片预览与临时保留", "外部图库", "存储健康", "主题与显示"]);
    assert.equal(await frame.locator("#settingsDirtyStatus").evaluate(element => { const probe = document.createElement("span"); probe.style.color = "var(--success)"; element.append(probe); const equal = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return equal; }), true);
    const sourceHeader = frame.locator("#externalSourcesPanel > .section-heading");
    await sourceHeader.scrollIntoViewIfNeeded();
    const plus = frame.locator("#addExternalSource");
    assert.equal(await plus.locator("svg").count(), 1);
    const headerBox = await sourceHeader.boundingBox(), plusBox = await plus.boundingBox(), svgBox = await plus.locator("svg").boundingBox();
    assert.ok(Math.abs(plusBox.y - headerBox.y) < 1 && Math.abs(plusBox.x + plusBox.width - headerBox.x - headerBox.width) < 1, JSON.stringify({ headerBox, plusBox }));
    assert.ok(Math.abs(plusBox.x + plusBox.width / 2 - svgBox.x - svgBox.width / 2) < 1 && Math.abs(plusBox.y + plusBox.height / 2 - svgBox.y - svgBox.height / 2) < 1);
    await frame.evaluate(() => window.ImageStudioAppearance.set({ glassOpacity: 0.4 }));
    await plus.click(); await frame.locator("#studioModal.is-external-editor").waitFor();
    const material = await frame.locator("#studioModal").evaluate(element => {
      const context = document.createElement("canvas").getContext("2d"); context.fillStyle = getComputedStyle(element).backgroundColor; context.fillRect(0, 0, 1, 1);
      return { alpha: context.getImageData(0, 0, 1, 1).data[3] / 255, blur: getComputedStyle(element).backdropFilter || getComputedStyle(element).webkitBackdropFilter, footer: getComputedStyle(element.querySelector("footer")).backdropFilter || getComputedStyle(element.querySelector("footer")).webkitBackdropFilter };
    });
    assert.ok(material.alpha > .38 && material.alpha < .42, JSON.stringify(material)); assert.ok(material.blur.includes("blur")); assert.equal(material.footer, "none");
    await frame.locator("#studioModalClose").click();
    const sortSaved = page.waitForResponse(response => response.url().includes("/gallery/preferences") && response.request().method() === "POST");
    await frame.locator("#gallerySort").selectOption("latest_content");
    await frame.locator("#saveSettingsButton").click(); await sortSaved;
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    await frame.waitForFunction(() => window.ImageStudioGalleryPreferences.getSort() === "latest_content");
    await frame.locator('[data-view="gallery"]').click(); await settle(frame);
    const latest = await refresh(); assert.equal(requests.at(-1).searchParams.get("sort"), "latest_content");
    const sequenceResponse = page.waitForResponse(response => response.url().includes("/gallery/image-sequence"));
    await frame.locator(".gallery-card .gallery-info").first().click();
    assert.equal(new URL((await sequenceResponse).url()).searchParams.get("sort"), "latest_content");
    await frame.locator("#closeDrawer").click();
    assert.ok(latest.items.every(item => typeof item.sort_time === "number"));

    // Restore defaults before providers/facets have loaded, including a value
    // currently absent from the gallery. The missing value must not imply all.
    await frame.evaluate(() => window.ImageStudioGalleryPreferences.setFilter("galleryProvider", { mode: "values", values: ["nai", "future-provider"] }));
    const beforeReload = requests.length; await open();
    assert.equal(requests[beforeReload].searchParams.get("provider_ids"), '["nai","future-provider"]');
    assert.equal(requests[beforeReload].searchParams.get("sort"), "latest_content");
    assert.deepEqual(await selected("galleryProvider"), ["nai"]);
    await refresh(); assert.equal(requests.at(-1).searchParams.get("provider_ids"), '["nai","future-provider"]');

    await trigger("galleryProvider").click();
    await frame.locator('[data-select-action="all"]').click();
    await frame.locator('[data-select-action="default"]').click();
    await frame.waitForFunction(() => window.ImageStudioGalleryPreferences.getFilter("galleryProvider")?.mode === "all");
    assert.deepEqual(await frame.evaluate(() => window.ImageStudioGalleryPreferences.getFilter("galleryProvider")), { mode: "all" });
    await trigger("galleryProvider").press("Escape");
    await page.route("**/gallery/list?*", async route => {
      const response = await route.fetch(), body = await response.json();
      body.filters.providers = [{ id: "", name: "未指定服务商" }, ...body.filters.providers, { id: "fresh-provider", name: "新增服务商" }];
      body.filters.generation_engines = ["unknown", ...body.filters.generation_engines];
      await route.fulfill({ response, json: body });
    });
    await refresh();
    assert.ok((await selected("galleryProvider")).includes("fresh-provider"));
    assert.equal((await values("galleryProvider")).at(-1), "");
    assert.equal((await values("galleryEngine")).at(-1), "unknown");
    await page.unroute("**/gallery/list?*");
    assert.deepEqual(errors, []);
    assert.ok(await frame.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    console.log(`${name}: facets, persistent defaults/sort, pager, responsive controls, card order and external glass passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [name, type, widths] of [["chromium", chromium, [1440, 390]], ["webkit", webkit, [390, 360]]]) {
    const browser = await type.launch({ headless: true });
    try { for (const width of widths) await verify(browser, `${name}-${width}`, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
