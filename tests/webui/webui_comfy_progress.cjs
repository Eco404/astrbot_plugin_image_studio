/* Real page queue restoration and polling; only ComfyUI job replies are mocked. */
const assert = require("node:assert/strict");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");

async function verify(browser, engine, width) {
  const page = await browser.newPage({ viewport: { width, height: 960 }, hasTouch: width < 600 });
  page.setDefaultTimeout(12000);
  const errors = [], polls = [];
  page.on("pageerror", error => errors.push(error.message));
  const jobs = new Map([
    ["multi", { id: "multi", model_name: "八张图片，三轮工作流", status: "running", created_at: 5, progress: { value: 18, max: 30, current: 2, completed: 1, total: 3 } }],
    ["single", { id: "single", model_name: "单次工作流", status: "running", created_at: 4, progress: { value: 7, max: 20, current: 1, completed: 0, total: 1 } }],
    ["unknown", { id: "unknown", model_name: "未知采样进度", status: "running", created_at: 3, progress: { completed: 0, total: 1 } }],
    ["waiting", { id: "waiting", model_name: "等待下一轮", status: "queued", created_at: 2, progress: { completed: 1, total: 3 } }],
    ["steps", { id: "steps", model_name: "仅采样进度", status: "running", created_at: 1, progress: { value: 0, max: 12 } }],
  ]);
  const status = (frame, name) => frame.locator(".comfy-job").filter({ has: frame.locator("strong").filter({ hasText: name }) }).locator('[role="status"]');
  async function expectStatus(frame, name, expected) {
    await status(frame, name).filter({ hasText: expected }).waitFor();
    assert.equal(await status(frame, name).textContent(), expected);
  }
  async function poll(frame) {
    await frame.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  }
  try {
    await page.route("**/comfy/jobs**", async route => {
      assert.equal(route.request().method(), "GET", "progress checks must not submit or mutate tasks");
      const id = new URL(route.request().url()).searchParams.get("id");
      if (id) polls.push(id);
      await route.fulfill({ json: id ? { job: jobs.get(id) } : { jobs: [...jobs.values()] } });
    });
    await page.goto(base);
    const frame = page.frames().find(item => item.url().includes("/ui/"));
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator("#comfyJobs:not([hidden])").waitFor();
    assert.equal(await frame.locator("#comfyJobs").evaluate(element => element.open), false);
    await frame.locator("#comfyJobs > summary").click();
    await expectStatus(frame, "八张图片，三轮工作流", "执行中 · 18/30 · 2/3");
    await expectStatus(frame, "单次工作流", "执行中 · 7/20");
    await expectStatus(frame, "未知采样进度", "执行中");
    await expectStatus(frame, "等待下一轮", "排队中 · 1/3");
    await expectStatus(frame, "仅采样进度", "执行中 · 0/12");
    assert.doesNotMatch(await frame.locator("#comfyJobList").textContent(), /分批|%/);

    // Another concurrent run can report fewer steps without changing completed
    // workflows. Its actual ordinal wins; waiting drops the sampling snapshot.
    jobs.get("multi").progress = { value: 2, max: 10, current: 3, completed: 1, total: 3 };
    jobs.get("single").progress = { current: 1, completed: 0, total: 1 };
    jobs.get("unknown").progress = { value: null, max: 0, total: 3 };
    await poll(frame);
    await expectStatus(frame, "八张图片，三轮工作流", "执行中 · 2/10 · 3/3");
    await expectStatus(frame, "单次工作流", "执行中");
    await expectStatus(frame, "未知采样进度", "执行中");
    jobs.get("multi").progress = { completed: 2, total: 3 };
    await poll(frame);
    await expectStatus(frame, "八张图片，三轮工作流", "执行中 · 2/3");

    // Defend against stale sampling fields in terminal responses as well.
    for (const [id, state] of [["multi", "succeeded"], ["single", "cancelled"], ["unknown", "unknown"], ["waiting", "failed"], ["steps", "partial"]]) {
      Object.assign(jobs.get(id), { status: state, progress: { value: 18, max: 30, current: 2, completed: id === "multi" ? 3 : 1, total: ["multi", "waiting"].includes(id) ? 3 : 1 } });
    }
    await poll(frame);
    await expectStatus(frame, "八张图片，三轮工作流", "已完成 · 3/3");
    await expectStatus(frame, "单次工作流", "已取消");
    await expectStatus(frame, "未知采样进度", "任务状态待核实");
    await expectStatus(frame, "等待下一轮", "失败 · 1/3");
    await expectStatus(frame, "仅采样进度", "部分完成");
    assert.doesNotMatch(await frame.locator("#comfyJobList").textContent(), /18\/30|分批|%/);
    assert.equal(await frame.locator("#comfyJobs").evaluate(element => element.open), true, "polling keeps the expanded queue open");
    assert.ok(polls.length >= jobs.size * 3, "state changes must flow through real queue polling");
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    assert.deepEqual(errors, []);
    console.log(`${engine} ${width}: sampler plus workflow progress, polling, unknown values and terminal cleanup passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of (process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"])) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
