/* Run against an isolated harness. Fixtures contain only synthetic landscapes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-batch-choices-"));
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);run=sys.argv[2];files={}
for index,kind in enumerate(("source","target","conflict","origins","mismatch","failure","later","removed","edited","cancelled")):
 # A transform after the observed output keeps these candidates manual so the
 # bulk-choice tests exercise adoption rather than automatic prompt completion.
 graph={
  "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":f"landscape-{kind}.safetensors"}},
  "2":{"class_type":"TextInput","inputs":{"text":f"static-{kind}"}},
  "3":{"class_type":"CustomTextTransform","inputs":{"text":["2",0],"operation":"runtime_transform"}},
  "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur, watermark"}},
  "30":{"class_type":"UnknownPostDisplayTransform","inputs":{"text":["3",1 if kind=="mismatch" else 0]}},
  "5":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["30",0]}},
  "6":{"class_type":"EmptyLatentImage","inputs":{"width":640,"height":480,"batch_size":1}},
  "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":41+index,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "8":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}},
  "9":{"class_type":"SaveImage","inputs":{"images":["8",0],"filename_prefix":f"branch-{kind}"}},
  "15":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":f"meadow-{kind}"}},
  "17":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["15",0],"negative":["4",0],"latent_image":["6",0],"seed":15,"steps":16,"cfg":5,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "18":{"class_type":"VAEDecode","inputs":{"samples":["17",0],"vae":["1",2]}},
  "19":{"class_type":"SaveImage","inputs":{"images":["18",0],"filename_prefix":"other_branch"}}
 }
 observers=("94",) if kind=="origins" else ("90","93")
 for node in observers:
  graph[node]={"class_type":"easy showAnything","inputs":{"anything":["3",0],"text":f"PREVIOUS_RUN_API_DISPLAY-{kind}"}}
 nodes=[];links=[]
 for key,node in graph.items():
  inputs=[]
  for field,value in node["inputs"].items():
   if isinstance(value,list):
    link_id=len(links)+1;links.append([link_id,int(value[0]),value[1],int(key),len(inputs),"STRING"])
    inputs.append({"name":field,"type":"STRING","link":link_id})
  widgets=["conflicting-ui-snapshot" if kind=="conflict" and key=="90" else f"rendered-{kind}"] if key in observers else []
  nodes.append({"id":int(key),"type":node["class_type"],"inputs":inputs,"widgets_values":widgets,"pos":[index*12,0],"size":[200,100]})
 workflow={"nodes":nodes,"links":links,"groups":[],"version":0.4,"extra":{"run":run,"kind":kind}}
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",json.dumps(graph));metadata.add_text("workflow",json.dumps(workflow))
 image=Image.new("RGB",(640,480),(174+index,204,210));draw=ImageDraw.Draw(image);draw.rectangle((0,300,640,480),fill=(112,146,136));draw.polygon([(0,320),(255,100),(505,320)],fill=(122,141,148))
 target=folder/f"{run}-{kind}.png";image.save(target,pnginfo=metadata);files[kind]=str(target)
print(json.dumps(files))
`;

async function waitUntil(check, message) {
  const end = Date.now() + 15000;
  while (Date.now() < end) { if (await check()) return; await new Promise((resolve) => setTimeout(resolve, 60)); }
  throw new Error(message);
}
async function noticeOff(frame) { if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click(); }
async function ready(frame) { await waitUntil(async () => !/正在/.test(await frame.locator("#importSummary").textContent()), "import did not settle"); }
async function expand(card) { const details = card.locator(".import-prompt-candidates"); if (!await details.evaluate((node) => node.open)) await details.locator(":scope > summary").click(); }
async function chooseOutput(page, card, value) {
  const select = card.locator("[data-import-output]");
  const index = await select.evaluate((node, target) => Array.from(node.options).findIndex((option) => option.value === target), value);
  const response = page.waitForResponse((item) => item.url().includes("/imports/inspect") && item.request().postDataJSON()?.output_node_id === value);
  await card.locator(".import-output-choice .studio-select-trigger").click();
  await page.frameLocator("#studio").locator(`.studio-select-menu [data-option-index="${index}"]`).click();
  await response; await ready(page.frameLocator("#studio"));
  await waitUntil(() => select.inputValue().then((selected) => selected === value), "output selection not committed");
}
async function rowByText(card, text) {
  const id = await card.locator(".prompt-candidate").evaluateAll((nodes, target) => nodes.find((node) => node.querySelector(".prompt-candidate-text > pre")?.textContent === target)?.dataset.promptCandidate, text);
  assert.ok(id, `missing candidate ${text}`); return card.locator(`[data-prompt-candidate="${id}"]`);
}
const single = (row, target = "prompt") => row.locator(`[data-candidate-target="${target}"]`);
const bulk = (row, target = "prompt") => row.locator(`[data-batch-candidate-target="${target}"]`);

async function verify(browser, name, width) {
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, name], { encoding: "utf8" }));
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = [], pending = []; let uploadCalls = 0, holdOutputs = false, fail = true, active = 0, peak = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => { if (/\/imports\/(prepare|upload|check)/.test(request.url())) uploadCalls++; });
  await page.route("**/imports/inspect", async (route) => {
    const body = route.request().postDataJSON();
    if (!body.output_node_id) { await route.continue(); return; }
    const kind = JSON.parse(body.metadata.workflow).extra.kind;
    if (fail && kind === "failure") { await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "synthetic branch read failure" }) }); return; }
    if (!holdOutputs) { await route.continue(); return; }
    active++; peak = Math.max(peak, active);
    await new Promise((resolve) => pending.push(resolve));
    active--; await route.continue();
  });
  const release = () => { for (const resolve of pending.splice(0)) resolve(); };
  try {
    await page.goto(base); const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="import"]').click();
    const card = (kind) => frame.locator(".import-card").filter({ has: frame.locator(`.import-card-header strong[data-tooltip="${path.basename(files[kind])}"]`) });
    const field = (kind, target = "prompt") => card(kind).locator(`[data-import-field="${target}"]`);
    await frame.locator("#importFiles").setInputFiles(["source", "target", "conflict", "origins", "mismatch", "failure"].map((kind) => files[kind]));
    await ready(frame);
    assert.equal(await card("source").locator("[data-batch-import-output]").isDisabled(), true);
    await chooseOutput(page, card("source"), "9");
    await chooseOutput(page, card("target"), "19");
    await field("target").fill("manual composition note");
    await card("source").locator("[data-batch-import-output]").click(); await ready(frame);
    assert.equal(await card("target").locator("[data-import-output]").inputValue(), "9");
    assert.equal(await field("target").inputValue(), "manual composition note", "output reparse must retain manual fields");
    assert.equal(await card("mismatch").locator("[data-import-output]").inputValue(), "", "same IDs with different output edges must not match");
    assert.equal(await card("failure").locator("[data-import-output]").inputValue(), "", "failed reparse retains original branch state");
    assert.match(await frame.locator("#importBatchSummary").textContent(), /已应用 3 张/);
    assert.match(await frame.locator("#importBatchSummary").textContent(), /已填入或已选择 1 张/);
    assert.match(await frame.locator("#importBatchSummary").textContent(), /读取失败 1 张/);
    await frame.locator("#importBatchSummary button").click();
    assert.match(await frame.locator("#studioModalBody").textContent(), /source\.png[\s\S]*已选择相同保存输出/);
    assert.match(await frame.locator("#studioModalBody").textContent(), /failure\.png[\s\S]*synthetic branch read failure/);
    await frame.locator("#studioModalClose").click();
    await noticeOff(frame); await card("source").locator(".import-output-choice").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${name}-output.png`) });
    await expand(card("source"));
    assert.match(await card("source").locator(".import-prompt-candidates").textContent(), /工作流执行回写候选/);
    assert.doesNotMatch(await card("source").locator(".import-prompt-candidates").textContent(), /PREVIOUS_RUN_API_DISPLAY/);
    assert.equal(await card("conflict").locator(".prompt-candidate-snapshot-note").count(), 2, "distinct workflow observers retain conflicting snapshots");
    let snapshot = await rowByText(card("source"), "rendered-source");
    assert.equal(await field("source").inputValue(), "");
    await bulk(snapshot).click(); await ready(frame);
    assert.equal(await field("source").inputValue(), "rendered-source", "bulk must apply to its own source card too");
    assert.equal(await single(snapshot).isDisabled(), true);
    assert.equal(await bulk(snapshot).isEnabled(), true, "already adopted source must still support bulk action");
    assert.equal(await field("target").inputValue(), "manual composition note\n\nrendered-target", "target must receive its own snapshot, not source text");
    assert.equal(await field("conflict").inputValue(), "", "merged source provenance splitting on target is ambiguous");
    assert.equal(await field("origins").inputValue(), "", "different observer origin must not match merely by output");
    assert.match(await frame.locator("#importBatchSummary").textContent(), /快照冲突 1 张/);
    await frame.locator("#importBatchSummary button").click();
    assert.match(await frame.locator("#studioModalBody").textContent(), /尚未选择保存分支/);
    await frame.locator("#studioModalClose").click();
    snapshot = await rowByText(card("source"), "rendered-source");
    await bulk(snapshot).click(); await ready(frame);
    assert.equal(await field("source").inputValue(), "rendered-source", "repeat must not duplicate source text");
    assert.equal((await field("target").inputValue()).split("rendered-target").length, 2, "repeat does not duplicate text");
    await field("target").fill("manual composition note\n\nrendered-target edited");
    await bulk(await rowByText(card("source"), "rendered-source")).click(); await ready(frame);
    assert.equal(await field("target").inputValue(), "manual composition note\n\nrendered-target edited");
    assert.match(await frame.locator("#importBatchSummary").textContent(), /手动修改保护 1 张/);
    await bulk(await rowByText(card("source"), "static-source")).click(); await ready(frame);
    assert.equal(await field("source").inputValue(), "rendered-source\n\nstatic-source");
    assert.equal(await field("conflict").inputValue(), "static-conflict");
    assert.equal(await field("origins").inputValue(), "static-origins");
    await bulk(await rowByText(card("source"), "static-source"), "negative_prompt").click(); await ready(frame);
    assert.equal(await field("source", "negative_prompt").inputValue(), "blur, watermark\n\nstatic-source");
    assert.equal(await field("conflict", "negative_prompt").inputValue(), "blur, watermark\n\nstatic-conflict");
    const splitBounds = await (await rowByText(card("source"), "static-source")).locator(".prompt-candidate-split").first().evaluate((node) => {
      const buttons = [...node.children].map((button) => { const box = button.getBoundingClientRect(); return { x: box.x, y: box.y, width: box.width, height: box.height }; });
      return { buttons, right: node.getBoundingClientRect().right, viewport: document.documentElement.clientWidth };
    });
    assert.ok(splitBounds.buttons[1].width >= 40 && splitBounds.buttons[1].height >= 40);
    assert.ok(splitBounds.buttons[0].x + splitBounds.buttons[0].width <= splitBounds.buttons[1].x + 1);
    assert.ok(splitBounds.right <= splitBounds.viewport + 1);
    await noticeOff(frame); await (await rowByText(card("source"), "static-source")).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${name}-split.png`) });

    // Keep requests pending while cards are added, edited, removed and cancelled.
    await frame.locator("#cancelImportButton").click();
    fail = false;
    await frame.locator("#importFiles").setInputFiles(["target", "removed", "edited", "source"].map((kind) => files[kind])); await ready(frame);
    await chooseOutput(page, card("source"), "9");
    holdOutputs = true;
    await card("source").locator("[data-batch-import-output]").click();
    await waitUntil(() => pending.length === 3, "expected bounded three parallel inspections");
    assert.equal(await frame.locator("#confirmImportButton").isDisabled(), true);
    assert.equal(await card("source").locator("[data-batch-import-output]").isDisabled(), true);
    await card("removed").locator("[data-remove-import]").click();
    await field("edited").evaluate((node) => { node.value = "edited while branch request pending"; node.dispatchEvent(new Event("input", { bubbles: true })); });
    await frame.locator("#importFiles").setInputFiles(files.later);
    await waitUntil(() => card("later").count().then(Boolean), "late addition missing");
    release(); holdOutputs = false; await ready(frame);
    assert.equal(await card("removed").count(), 0);
    assert.equal(await field("edited").inputValue(), "edited while branch request pending");
    assert.equal(await card("edited").locator("[data-import-output]").inputValue(), "", "changed item must not commit async result");
    assert.equal(await card("later").locator("[data-import-output]").inputValue(), "", "newly added item must not inherit old batch selection");
    assert.equal(await card("target").locator("[data-import-output]").inputValue(), "9");
    assert.ok(peak <= 3);
    await frame.locator("#importFiles").setInputFiles(files.cancelled); await ready(frame);
    holdOutputs = true;
    await card("source").locator("[data-batch-import-output]").click();
    await waitUntil(() => pending.length > 0, "expected pending output before cancel");
    await frame.locator("#cancelImportButton").click();
    release(); holdOutputs = false;
    await waitUntil(() => frame.locator(".import-card").count().then((count) => count === 0), "cancel should clear pending cards");
    await frame.locator("#importFiles").setInputFiles(files.later); await ready(frame);
    assert.equal(await card("later").locator("[data-import-output]").inputValue(), "");
    assert.equal(await frame.locator("#importBatchSummary").isVisible(), false, "cancelled job cannot publish stale results");
    await chooseOutput(page, card("later"), "9");
    assert.equal(await card("later").locator("[data-batch-import-output]").isEnabled(), true, "apply all is valid for one pending image");
    await card("later").locator("[data-batch-import-output]").click(); await ready(frame);
    assert.match(await frame.locator("#importBatchSummary").textContent(), /已填入或已选择 1 张/);
    await expand(card("later"));
    snapshot = await rowByText(card("later"), "rendered-later");
    await bulk(snapshot).click(); await ready(frame);
    assert.equal(await field("later").inputValue(), "rendered-later", "single-image apply all must populate the current image");
    await bulk(snapshot).click(); await ready(frame);
    assert.equal(await field("later").inputValue(), "rendered-later");
    assert.equal(uploadCalls, 0, "all actions remain draft-only");
    const inner = page.frames().find((entry) => entry.url().includes("/ui/"));
    assert.ok(await inner.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
    assert.deepEqual(errors, []);
    console.log(`${name}: own-text matching, split controls, provenance conflicts, branch selection, failures, manual protection, bounded concurrency and cancellation passed`);
  } finally { release(); await page.close(); }
}

(async () => {
  for (const [engine, browserType] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await browserType.launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, `${engine}-${width}`, width); }
    finally { await browser.close(); }
  }
  console.log(`Batch-choice screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
