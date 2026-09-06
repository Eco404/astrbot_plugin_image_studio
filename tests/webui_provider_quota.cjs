/* Mock quota and generation requests; only the isolated harness bootstrap/settings are read. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-provider-quota-"));
const refs = { natural: "natural:studio-image", a: "nai:nai-diffusion-4-5-full", a2: "nai:quota-second", b: "quota-b:nai-diffusion-4-5-full", b2: "quota-b:quota-second" };

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function bounded(promise, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label}: timed out`)), 12000); })]);
  } finally { clearTimeout(timer); }
}

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function choose(frame, modelRef) {
  const select = frame.locator("#modelChoice");
  const index = await select.evaluate((element, desired) => Array.from(element.options).findIndex((option) => option.value === desired), modelRef);
  assert.ok(index >= 0, `missing model choice ${modelRef}`);
  await frame.locator('.studio-select-trigger[data-select-id="modelChoice"]').click();
  await frame.locator(`.studio-select-menu [data-option-index="${index}"]`).click();
  assert.equal(await select.inputValue(), modelRef);
}

async function quotaText(frame, remaining, enabled = true) {
  const expected = `剩余额度 ${remaining.toLocaleString("zh-CN")}${enabled ? "" : " · 已停用"}`;
  await frame.waitForFunction((value) => document.getElementById("providerQuota")?.textContent === value, expected);
  assert.equal(await frame.locator("#providerQuota").isVisible(), true);
}

async function fresh(frame, event = "focus") {
  await frame.evaluate((event) => {
    window.__quotaTimeOffset += 31000;
    if (event === "visibilitychange") document.dispatchEvent(new Event(event));
    else window.dispatchEvent(new Event(event));
  }, event);
}

async function capture(page, frame, name) {
  await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await settle(frame);
  const box = await frame.evaluate(() => {
    const quota = document.getElementById("providerQuota"); const status = document.getElementById("providerStatus"); const header = document.querySelector(".topbar");
    return { width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth, quota: quota.getBoundingClientRect().toJSON(), status: status.getBoundingClientRect().toJSON(), header: header.getBoundingClientRect().toJSON(), nameDisplay: getComputedStyle(document.getElementById("providerStatusName")).display };
  });
  if (box.scroll > box.width + 1) {
    await page.screenshot({ path: path.join(output, `${name}-overflow.png`) });
    const overflowing = await frame.evaluate(() => {
      const elements = Array.from(document.querySelectorAll("body *")).filter((element) => element.getClientRects().length && (element.getBoundingClientRect().right > document.documentElement.clientWidth + 1 || element.scrollWidth > element.clientWidth + 3)).map((element) => ({ tag: element.tagName, id: element.id, class: element.className, right: element.getBoundingClientRect().right, width: element.getBoundingClientRect().width, scroll: element.scrollWidth, client: element.clientWidth, overflow: getComputedStyle(element).overflow, display: getComputedStyle(element).display }));
      const candidates = ["#modelChoice", "#modelProvider", "#providerStatus", ".studio-select-value"];
      const probes = candidates.map((selector) => { const element = document.querySelector(selector); const old = element.style.display; element.style.display = "none"; const width = document.documentElement.scrollWidth; element.style.display = old; return { selector, width }; });
      return { elements, probes };
    });
    assert.fail(`${name}: horizontal overflow ${JSON.stringify({ ...box, overflowing, output })}`);
  }
  assert.ok(box.quota.x >= box.header.x && box.quota.right <= box.header.right + 1 && box.quota.bottom <= box.header.bottom + 1, `${name}: quota escapes title card ${JSON.stringify(box)}`);
  if (box.width <= 540) assert.equal(box.nameDisplay, "none", `${name}: mobile status should prioritize quota`);
  else assert.notEqual(box.nameDisplay, "none");
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function setup(browser, test, clock = false) {
  const context = await browser.newContext({ viewport: { width: test.width, height: test.width <= 540 ? 844 : 1000 }, hasTouch: test.width <= 540, reducedMotion: test.theme === "dark" ? "reduce" : "no-preference" });
  await context.addInitScript(() => {
    window.__quotaTimeOffset = 0;
    const realNow = Date.now.bind(Date);
    Date.now = () => realNow() + window.__quotaTimeOffset;
  });
  const page = await context.newPage(); page.setDefaultTimeout(12000);
  if (clock) await page.clock.install();
  const errors = []; const calls = []; const queues = new Map(); const holds = []; let bootstrapCount = 0; let settingsSaveCount = 0;
  let generation = { fail: false, wait: null }; const generations = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error" && !message.text().includes("503 (Service Unavailable)")) errors.push(message.text()); });
  await page.route("**/studio/bootstrap", async (route) => {
    const response = await route.fetch(); const payload = await response.json(); bootstrapCount++;
    const source = payload.models.find((model) => model.provider_id === "nai");
    const a = payload.providers.find((provider) => provider.id === "nai");
    const second = { ...structuredClone(source), id: "quota-second", name: "NAI 第二模型", model_ref: refs.a2 };
    a.models.push(structuredClone(second)); payload.models.push(second);
    const b = { ...structuredClone(a), id: "quota-b", name: "备用 NAI 额度服务商 - 较长名称与多模型能力检查" };
    payload.providers.push(b);
    payload.models.push(...[source, second].map((model) => ({ ...structuredClone(model), provider_id: b.id, provider_name: b.name, model_ref: `${b.id}:${model.id}` })));
    payload.defaults = { text2img_model_ref: refs.natural, img2img_model_ref: refs.natural };
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/studio/provider-quota?*", async (route) => {
    const providerId = new URL(route.request().url()).searchParams.get("provider_id");
    calls.push(providerId);
    const plan = queues.get(providerId)?.shift();
    assert.ok(plan, `unexpected quota request ${providerId} (#${calls.length})`);
    plan.started.resolve();
    if (plan.hold) await plan.hold.promise;
    try {
      const data = plan.failure ? { message: "测试额度服务暂不可用" } : { provider_id: providerId, remaining: plan.remaining, enabled: plan.enabled, checked_at: Date.now() / 1000 };
      await route.fulfill({ status: plan.failure ? 503 : 200, contentType: "application/json", body: JSON.stringify(data) });
    } finally { plan.done.resolve(); }
  });
  await page.route("**/studio/generate", async (route) => {
    generations.push(route.request().postDataJSON());
    generation.started?.resolve();
    if (generation.wait) await generation.wait.promise;
    const data = generation.fail ? { message: "测试生图失败" } : { images: [], provider_name: "NAI 测试", model: "nai-diffusion-4-5-full", elapsed_ms: 1 };
    await route.fulfill({ status: generation.fail ? 503 : 200, contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.route("**/settings/save", async (route) => { settingsSaveCount++; await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true }) }); });
  function plan(providerId, remaining, options = {}) {
    const item = { remaining, enabled: options.enabled !== false, failure: !!options.failure, started: deferred(), done: deferred(), hold: options.hold ? deferred() : null };
    if (!queues.has(providerId)) queues.set(providerId, []);
    queues.get(providerId).push(item); if (item.hold) holds.push(item.hold);
    return item;
  }
  await page.goto(base); assert.equal(await page.locator("#studio").count(), 1, "expected isolated harness");
  const frame = page.frames().find((item) => item.url().includes("/ui/"));
  await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
  await frame.waitForFunction(() => document.documentElement.dataset.appearanceReady === "true");
  await frame.evaluate(async (theme) => { await ImageStudioAppearance.ready; ImageStudioAppearance.set({ preference: theme }); await ImageStudioAppearance.saved(); }, test.theme);
  return { page, frame, context, calls, errors, plan, generations, setGeneration: (value) => { generation = value; }, bootstrapCount: () => bootstrapCount, settingsSaveCount: () => settingsSaveCount, close: async () => { holds.forEach((hold) => hold.resolve()); await context.close(); } };
}

async function matrix(browser, test) {
  const name = `${engine}-${test.width}-${test.theme}`;
  const run = await setup(browser, test);
  const { page, frame, calls, plan } = run;
  try {
    assert.equal(await frame.locator("#providerQuota").isVisible(), false); assert.deepEqual(calls, []);
    const pendingA = plan("nai", 943, { hold: true });
    await choose(frame, refs.a); await bounded(pendingA.started.promise, "first NAI query");
    await frame.locator("#providerQuota").filter({ hasText: "额度查询中" }).waitFor();
    await choose(frame, refs.a2); assert.deepEqual(calls, ["nai"], "same-provider pending requests must be deduplicated");
    plan("quota-b", 82); await choose(frame, refs.b); await quotaText(frame, 82);
    pendingA.hold.resolve(); await bounded(pendingA.done.promise, "late NAI response"); await settle(frame); await quotaText(frame, 82);
    await capture(page, frame, `${name}-long-provider`);
    await choose(frame, refs.b2); await quotaText(frame, 82); assert.equal(calls.length, 2);
    await choose(frame, refs.a); await quotaText(frame, 943); assert.equal(calls.length, 2, "provider result should cache across model/provider switches");
    await capture(page, frame, `${name}-quota`);
    const late = plan("nai", 900, { hold: true }); await fresh(frame); await bounded(late.started.promise, "expired quota query");
    await choose(frame, refs.natural); late.hold.resolve(); await bounded(late.done.promise, "late quota after natural model"); await settle(frame);
    assert.equal(await frame.locator("#providerQuota").isVisible(), false, "late NAI result reappeared for natural-language model");
    await choose(frame, refs.a); await quotaText(frame, 900);
    const unselected = plan("nai", 850, { hold: true }); await fresh(frame); await bounded(unselected.started.promise, "unselected quota query");
    await choose(frame, ""); unselected.hold.resolve(); await bounded(unselected.done.promise, "quota after unselection"); await settle(frame);
    assert.equal(await frame.locator("#providerQuota").isVisible(), false, "late quota reappeared without a model");
    assert.equal(await frame.locator("#generatorWorkspace").evaluate((fieldset) => fieldset.disabled), true);
    await choose(frame, refs.a); await quotaText(frame, 850);
    plan("nai", 0, { enabled: false }); await fresh(frame, "visibilitychange"); await quotaText(frame, 0, false);
    await capture(page, frame, `${name}-zero-disabled`);
    assert.equal(await frame.locator("#generateButton").isEnabled(), true, "quota status must not disable the generation form");
    plan("nai", 0, { failure: true }); await fresh(frame); await frame.locator("#providerQuota").filter({ hasText: "额度暂不可用" }).waitFor();
    assert.match(await frame.locator("#providerQuota").getAttribute("title"), /测试额度服务暂不可用/);
    assert.equal(await frame.locator("#generatorWorkspace").evaluate((fieldset) => fieldset.disabled), false);
    assert.equal(await frame.locator("#generationError").textContent(), "");
    assert.equal(await frame.locator("#appNotice").isVisible(), false, "quota failure must stay local to the status");
    await capture(page, frame, `${name}-unavailable`);
    await frame.locator("#prompt").fill("safe landscape quota browser fixture");
    for (const failure of [false, true]) {
      run.setGeneration({ fail: failure }); plan("nai", failure ? 811 : 812);
      await frame.locator("#generateButton").click(); await quotaText(frame, failure ? 811 : 812);
      assert.equal(await frame.locator("#generateButton").isEnabled(), true);
      if (failure) assert.match(await frame.locator("#generationError").textContent(), /测试生图失败/);
    }
    assert.equal(run.generations.length, 2);
    const generationWait = deferred(), generationStarted = deferred();
    run.setGeneration({ fail: false, wait: generationWait, started: generationStarted });
    await frame.locator("#generateButton").click(); await bounded(generationStarted.promise, "deferred generation");
    plan("quota-b", 75); await choose(frame, refs.b); await quotaText(frame, 75);
    const beforeLateGeneration = calls.length;
    generationWait.resolve(); await frame.locator("#generateButton:not(:disabled)").waitFor(); await settle(frame);
    await quotaText(frame, 75); assert.equal(calls.length, beforeLateGeneration, "old provider generation completion queried or overwrote the newly selected provider");
    plan("nai", 810); await choose(frame, refs.a); await quotaText(frame, 810);
    const settingsCalls = calls.length;
    await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await frame.locator('[data-view="settings"]').click();
    await frame.locator("#settingsDirtyStatus").filter({ hasText: "已保存" }).waitFor();
    await frame.evaluate(() => window.dispatchEvent(new Event("focus"))); await settle(frame); assert.equal(calls.length, settingsCalls, "inactive generator view queried quota");
    assert.equal(await frame.locator("#providerQuota").isVisible(), false);
    await frame.locator("#saveSettingsButton").click();
    await frame.locator("#appNoticeMessage").filter({ hasText: "设置已保存并生效" }).waitFor();
    assert.equal(run.settingsSaveCount(), 1); assert.equal(run.bootstrapCount(), 2);
    await frame.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await frame.locator('[data-view="generate"]').click();
    plan("nai", 777); await choose(frame, refs.a); await quotaText(frame, 777);
    assert.ok(calls.every((id) => id === "nai" || id === "quota-b"), "natural provider must not be queried");
    assert.deepEqual(run.errors, [], `${name}: browser errors`);
    console.log(`${name}: quota races, provider cache, zero/disabled/error, generation refresh and bootstrap invalidation passed`);
  } finally { await run.close(); }
}

async function periodic(browser) {
  const run = await setup(browser, { width: 390, theme: "light" }, true);
  const { page, frame, plan, calls } = run;
  try {
    plan("nai", 100); await choose(frame, refs.a); await quotaText(frame, 100);
    const next = plan("nai", 99);
    await page.clock.fastForward(61000); await bounded(next.started.promise, "periodic quota query"); await quotaText(frame, 99);
    await frame.locator('[data-view="gallery"]').click(); await frame.locator(".gallery-card").first().waitFor();
    await page.clock.fastForward(31000); assert.equal(calls.length, 2, "periodic refresh ran outside generation view");
    plan("nai", 98); await frame.locator('[data-view="generate"]').click(); await quotaText(frame, 98);
    assert.deepEqual(run.errors, []);
    console.log(`${engine}: periodic 30-second visible-view refresh passed`);
  } finally { await run.close(); }
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    const widths = process.env.STUDIO_QUOTA_WIDTHS ? process.env.STUDIO_QUOTA_WIDTHS.split(",").map(Number) : [1440, 390, 320];
    for (const width of widths) for (const theme of ["light", "dark"]) await matrix(browser, { width, theme });
    await periodic(browser);
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
