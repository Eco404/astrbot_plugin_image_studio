/* Gallery actions follow the selected image's engine, without provider calls. */
const assert = require("node:assert/strict");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const imageUrl = "data:image/svg+xml;base64," + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="#b3c6bb"/></svg>').toString("base64");

async function verify(engine, width) {
  const browser = await engine.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height: 900 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], copies = [], actionRequests = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => { if (/\/gallery\/(parameters|favorite|delete)|\/studio\/reference\/from-gallery/.test(request.url())) actionRequests.push(request.url()); });
  await page.addInitScript(() => {
    let factory;
    Object.defineProperty(window, "ImageStudioLibrary", { configurable: true, get: () => factory, set(value) {
      factory = hooks => { window.__detailState = hooks.state; window.__detailHooks = hooks; return window.__detailLibrary = value(hooks); };
    } });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async content => { window.__copiedDetail = content; } } });
  });
  const response = await page.request.get(`${base}/astrbot_plugin_image_studio/gallery/list?limit=24`);
  assert.ok(response.ok());
  const payload = await response.json(), cards = payload.items.slice(0, 4);
  assert.equal(cards.length, 4, "isolated harness supplies gallery records");
  const mixed = [
    { engine: "comfyui", format: "comfyui", formats: ["workflow", "comfy_api"], raw: { workflow: { nodes: [] }, prompt: { "1": { class_type: "SaveImage", inputs: {} } } } },
    { engine: "openai_images", format: "unknown", formats: ["studio"], mode: "img2img" },
    { engine: "unknown", format: "a1111", formats: ["a1111"], raw: { parameters: "a forest\nSteps: 20" } },
    { engine: "unknown", format: "unknown", formats: [], empty: true },
    { engine: "unknown", format: "novelai", formats: ["studio", "nai", "novelai"], raw: { Comment: { prompt: "a forest" } } },
    { engine: "gemini", format: "unknown", formats: ["studio"] },
    { engine: "custom_json", format: "unknown", formats: ["studio"] },
    { engine: "nai", format: "novelai", formats: ["studio", "nai", "novelai"], raw: { Comment: { prompt: "a forest" } } },
  ];
  const groups = new Map(), images = new Map(), gates = new Map();
  const groupSpecs = [
    { source: "import", generation_engine: "mixed", provider_kind: "import", items: mixed },
    { source: "external", generation_engine: "unknown", external_source: { id: "directory-test", name: "普通目录", type: "directory" }, items: [mixed[0]] },
    { source: "external", generation_engine: "unknown", provider_kind: "external", external_source: { id: "nai-test", name: "NAI 插件图库", type: "nai" }, items: [mixed[4]] },
    { source: "webui", generation_engine: "unknown", provider_kind: "nai_direct", items: [{ format: "unknown", formats: ["studio", "nai", "novelai"] }] },
  ];
  groupSpecs.forEach((spec, groupIndex) => {
    const id = cards[groupIndex].id;
    const group = { id, created_at: 1700000000, source: spec.source, is_external: spec.source === "external", generation_engine: spec.generation_engine, provider_kind: spec.provider_kind || "", external_source: spec.external_source, provider_id: "not-configured", provider_name: "测试来源", mode: "text2img", model: "not-configured-model", original_prompt: "inferred external request", parameters: {}, references: [], supplemental: {}, lightweight: true, images: [] };
    spec.items.forEach((item, index) => {
      const imageId = `e${groupIndex}${index}`.padEnd(32, "0");
      const record = { id: imageId, generation_id: id, ordinal: index, width: 400, height: 300, mime_type: "image/png", size_bytes: 128, sha256: imageId.padEnd(64, "a"), file_state: "available", download_filename: `image-${index}.png`, mode: item.mode || "text2img", model: "not-configured-model" };
      group.images.push(record);
      images.set(imageId, { group, index, spec: item, record, metadata: { format: item.format, normalized: item.empty ? {} : { prompt: "parsed image prompt", model: "not-configured-model", steps: 20 }, raw: item.raw || {}, warnings: [] }, supplemental: { generation_engine: item.engine, prompt: "inferred external request", mode: record.mode } });
      let release;
      const promise = new Promise(resolve => { release = resolve; });
      gates.set(imageId, { promise, release });
    });
    groups.set(id, group);
  });
  await page.route("**/gallery/detail/**", async route => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    if (!groups.has(id)) return route.fallback();
    return route.fulfill({ json: groups.get(id) });
  });
  await page.route("**/gallery/image-info/**", async route => {
    const id = new URL(route.request().url()).pathname.split("/").pop(), entry = images.get(id);
    if (!entry) return route.fallback();
    if (gates.has(id)) await gates.get(id).promise;
    const { images: _images, ...detailFields } = entry.group;
    return route.fulfill({ json: { image: { ...entry.record, metadata: entry.metadata, supplemental: entry.supplemental }, detail_fields: detailFields } });
  });
  await page.route("**/gallery/image/**", route => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    return images.has(id) ? route.fulfill({ json: { id, data_url: imageUrl, thumbnail_data_url: imageUrl } }) : route.fallback();
  });
  await page.route("**/gallery/parameters/**", route => {
    const url = new URL(route.request().url()), format = url.searchParams.get("format"), imageId = url.searchParams.get("image_id");
    copies.push({ format, imageId });
    return route.fulfill({ json: { format, filename: `${format}.json`, content: JSON.stringify({ copied_format: format, image_id: imageId }) } });
  });
  try {
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="gallery"]').click();
    const group = groups.get(cards[0].id);
    const footerSnapshot = () => frame.locator("#detailFooter").evaluate(footer => [...footer.querySelectorAll("button, .copy-format-control")].map(element => ({
      id: element.id || element.className, hidden: element.hidden, disabled: element.disabled,
      width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, opacity: getComputedStyle(element).opacity,
    })));
    const checkPendingSafety = async () => {
      const previousRequests = actionRequests.length;
      assert.equal(await frame.locator("#detailFooter").evaluate(footer => footer.inert), true);
      assert.equal(await frame.locator("#detailFooter").getAttribute("aria-busy"), "true");
      await frame.locator("#detailFooter").evaluate(footer => footer.querySelectorAll("button").forEach(button => button.click()));
      await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(actionRequests.length, previousRequests, "pending footer cannot act on a previous image, including synthetic clicks");
      assert.equal(await frame.locator("#studioModalRoot").evaluate(root => root.classList.contains("is-hidden")), true);
      assert.equal(await frame.evaluate(() => window.__detailState.view), "gallery");
    };
    for (let index = 0; index < mixed.length; index++) {
      const id = group.images[index].id, expected = mixed[index];
      const previousLayout = index ? await footerSnapshot() : null;
      await frame.evaluate(() => { window.__footerOptionNodes = [...document.getElementById("detailCopyFormat").options]; });
      const request = page.waitForRequest(request => new URL(request.url()).pathname.endsWith(`/gallery/image-info/${id}`));
      if (!index) await frame.locator(`[data-gallery-id="${group.id}"] .gallery-info`).click();
      else await frame.locator(`[data-detail-dot="${index}"]`).evaluate(button => button.click());
      await request;
      if (previousLayout) assert.deepEqual(await footerSnapshot(), previousLayout, "retain footer layout and opacity until current metadata is ready");
      else {
        assert.equal(await frame.locator("#detailReproduce").isVisible(), false, "the first load does not guess a capability");
        assert.equal(await frame.locator('#detailCopyFormat option[value="studio"]').count(), 0);
      }
      await checkPendingSafety();
      gates.get(id).release();
      await frame.waitForFunction(id => window.__detailState.detailData?.images?.find(image => image.id === id)?._metadataLoaded, id);
      assert.equal(await frame.locator("#detailFooter").evaluate(footer => footer.inert), false);
      if (index === 6) assert.ok(await frame.evaluate(() => window.__footerOptionNodes.every((node, index) => node === document.getElementById("detailCopyFormat").options[index])), "same-format images preserve native option nodes");
      assert.deepEqual(await frame.locator("#detailCopyFormat").evaluate(select => [...select.options].map(option => option.value)), expected.formats);
      assert.equal(await frame.locator("#detailReproduce").isVisible(), expected.formats.includes("studio"));
      assert.equal(await frame.locator("#detailCopy").isVisible(), expected.formats.length > 0);
      assert.equal(await frame.locator(".copy-format-control").evaluate(element => element.hidden), !expected.formats.length);
      assert.equal(await frame.locator("#detailWorkflowDownload").isVisible(), expected.format === "comfyui");
      if (expected.formats.length) {
        assert.equal(await frame.locator("#detailCopyFormat").inputValue(), expected.formats[0]);
        await frame.locator("#detailCopy").click();
        await frame.waitForFunction(id => window.__copiedDetail?.includes(id), id);
        assert.deepEqual(copies.at(-1), { format: expected.formats[0], imageId: id });
      }
    }
    await frame.locator("#detailCopyFormat").selectOption("novelai");
    await frame.locator('[data-select-id="detailCopyFormat"].studio-select-trigger').click();
    await frame.evaluate(() => {
      window.__footerOptionNodes = [...document.getElementById("detailCopyFormat").options];
      window.__footerMenu = document.querySelector('.studio-select-menu:not([hidden])');
      window.__footerMenuRows = [...window.__footerMenu.querySelectorAll('[role="option"]')];
      for (let count = 0; count < 5; count++) window.__detailLibrary.updateDetailActions(window.__detailState.detailData);
    });
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.ok(await frame.evaluate(() => window.__footerOptionNodes.every((node, index) => node === document.getElementById("detailCopyFormat").options[index])));
    assert.ok(await frame.evaluate(() => window.__footerMenu === document.querySelector('.studio-select-menu:not([hidden])') && window.__footerMenuRows.every((node, index) => node === window.__footerMenu.querySelectorAll('[role="option"]')[index])), "repeated asset callbacks preserve the open format menu");
    assert.equal(await frame.locator("#detailCopyFormat").inputValue(), "novelai");
    await frame.evaluate(() => window.ImageStudioSelect.close());
    // Cached navigation must restore supported/unsupported states too.
    await frame.locator('[data-detail-dot="0"]').evaluate(button => button.click());
    await frame.locator('#detailCopyFormat option[value="workflow"]').waitFor({ state: "attached" });
    assert.equal(await frame.locator("#detailReproduce").isVisible(), false);
    assert.equal(await frame.locator("#detailCopyFormat").inputValue(), "workflow");
    for (let groupIndex = 1; groupIndex < groupSpecs.length; groupIndex++) {
      const group = groups.get(cards[groupIndex].id), id = group.images[0].id;
      const previousLayout = groupIndex === 1 ? await footerSnapshot() : null;
      const request = page.waitForRequest(request => new URL(request.url()).pathname.endsWith(`/gallery/image-info/${id}`));
      if (groupIndex === 1) await frame.evaluate(id => { void window.__detailHooks.openDetail(id); }, group.id);
      else await frame.locator(`[data-gallery-id="${group.id}"] .gallery-info`).click();
      await request;
      if (previousLayout) assert.deepEqual(await footerSnapshot(), previousLayout, "cross-group loading keeps the resolved footer visible");
      else {
        assert.equal(await frame.locator("#detailReproduce").isVisible(), false, "reopening a closed drawer discards old actions");
        assert.equal(await frame.locator('#detailCopyFormat option[value="studio"]').count(), 0);
      }
      await checkPendingSafety();
      gates.get(id).release();
      await frame.waitForFunction(id => window.__detailState.detailData?.images?.find(image => image.id === id)?._metadataLoaded, id);
      if (groupIndex === 1) {
        assert.equal(await frame.locator("#drawerBody h3").filter({ hasText: "外部图片参数" }).count(), 0);
        const generated = frame.locator(".generated-parameters");
        assert.equal(await generated.evaluate(element => element.tagName), "DIV");
        assert.equal(await generated.locator("summary").count(), 0);
        assert.equal(await frame.locator("#drawerBody > .detail-block").first().evaluate(element => element.classList.contains("generated-parameters")), true);
        assert.ok((await generated.innerText()).includes("parsed image prompt"));
        assert.ok(!(await frame.locator("#drawerBody").innerText()).includes("inferred external request"));
        await generated.locator("[data-copy-field]").first().click();
        assert.equal(await frame.evaluate(() => window.__copiedDetail), "parsed image prompt");
      } else if (groupIndex === 2) {
        assert.equal(await frame.locator("#drawerBody h3").filter({ hasText: "外部图片参数" }).count(), 1);
        assert.equal(await frame.locator(".generated-parameters").evaluate(element => element.tagName), "DETAILS");
        assert.equal(await frame.locator(".generated-parameters").getAttribute("open"), null);
        assert.equal(await frame.locator("#detailReproduce").isVisible(), true, "placeholder provider_kind must not shadow recognized image metadata");
      } else {
        assert.equal(await frame.locator("#detailReproduce").isVisible(), true, "legacy plugin records infer capability from provider_kind");
        assert.equal(await frame.locator('#detailCopyFormat option[value="studio"]').count(), 1);
      }
      await frame.locator("#closeDrawer").click();
    }
    assert.deepEqual(errors, []);
    console.log(`${width}px: stable pending footer, inert safety, option/menu reuse, engine-aware actions and external metadata layout passed`);
  } finally {
    gates.forEach(gate => gate.release());
    await page.close(); await browser.close();
  }
}

(async () => { await verify(chromium, 1440); await verify(webkit, 390); })().catch(error => { console.error(error); process.exitCode = 1; });
