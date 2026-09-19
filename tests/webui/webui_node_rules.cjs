/* Only synthetic PNGs and the isolated harness are used. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-node-rules-"));
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];files=[]
for index in range(2):
 graph={
  "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"landscape.safetensors"}},
  "3":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["10",0]}},
  "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur"}},
  "6":{"class_type":"EmptyLatentImage","inputs":{"width":320,"height":240,"batch_size":1}},
  "10":{"class_type":f"ManualTextJoin-{marker}","inputs":{"text_a":f"landscape-{index}","text_b":"daylight","option":"strict"}},
  "11":{"class_type":f"ManualTextDisplay-{marker}","inputs":{"text":["10",0]}},
  "19":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["3",0],"negative":["4",0],"latent_image":["6",0],"seed":42+index,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "20":{"class_type":"VAEDecode","inputs":{"samples":["19",0],"vae":["1",2]}},
  "21":{"class_type":"SaveImage","inputs":{"images":["20",0],"filename_prefix":"test"}}
 }
 nodes=[];links=[]
 for identifier,node in graph.items():
  inputs=[]
  for field,value in node["inputs"].items():
   if isinstance(value,list):
    link_id=len(links)+1;links.append([link_id,int(value[0]),value[1],int(identifier),len(inputs),"STRING"])
    inputs.append({"name":field,"type":"STRING","link":link_id})
   else: inputs.append({"name":field,"type":"STRING" if field in ["text_a","text_b"] else "unknown","link":None})
  if identifier=="10": inputs.append({"name":"optional_text","type":"STRING","link":None})
  outputs=[{"name":"text","type":"STRING","links":[]}]
  if identifier=="10": outputs.append({"name":"unused_text","type":"STRING","links":[]})
  widgets=[f"landscape-{index}, daylight"] if identifier=="11" else []
  nodes.append({"id":int(identifier),"type":node["class_type"],"inputs":inputs,"outputs":outputs,"widgets_values":widgets,"mode":0})
 for link_id,source,port,target,target_port,kind in links:
  source_node=next(node for node in nodes if node["id"]==source)
  while len(source_node["outputs"])<=port: source_node["outputs"].append({"name":"extra","type":"unknown","links":[]})
  source_node["outputs"][port]["links"].append(link_id)
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",json.dumps(graph));metadata.add_text("workflow",json.dumps({"nodes":nodes,"links":links,"version":0.4,"id":marker}))
 target=folder/f"{marker}-{index}.png";Image.new("RGB",(320,240),(175+index,204,210)).save(target,pnginfo=metadata);files.append(str(target))
plain=folder/f"{marker}-plain.png";Image.new("RGB",(320,240),(160,180,190)).save(plain)
print(json.dumps({"files":files,"plain":str(plain)}))
`;

async function until(check, message) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) { if (await check()) return; await new Promise((resolve) => setTimeout(resolve, 50)); }
  throw new Error(message);
}
async function choose(frame, id, value) {
  await frame.locator(id).evaluate((select, target) => { select.value = target; select.dispatchEvent(new Event("input", { bubbles: true })); select.dispatchEvent(new Event("change", { bubbles: true })); }, value);
}
async function run(browser, engine, width) {
  const marker = `${path.basename(output)}-${engine}-${width}`;
  const { files, plain } = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker], { encoding: "utf8" }));
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="import"]').click();
    const rulesButton = frame.locator("#importNodeRulesButton");
    assert.equal(await rulesButton.isVisible(), false, "No rule management button without ComfyUI images");
    await frame.locator("#importFiles").setInputFiles(plain);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    assert.equal(await rulesButton.isVisible(), false, "Plain images do not show ComfyUI rule actions");
    await frame.locator("#cancelImportButton").click();
    await frame.locator("#importFiles").setInputFiles(files);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    const first = frame.locator("#importGrid .import-card").nth(0), second = frame.locator("#importGrid .import-card").nth(1);
    assert.equal(await rulesButton.isVisible(), true);
    assert.equal(await rulesButton.evaluate(node => node.parentElement.classList.contains("import-floatingbar") && node.parentElement.firstElementChild === node), true);
    const bounds = await rulesButton.evaluate(node => ({ right: node.getBoundingClientRect().right, next: document.getElementById("cancelImportButton").getBoundingClientRect().left, page: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert.ok(bounds.right < bounds.next && bounds.page <= bounds.viewport + 1, JSON.stringify(bounds));
    const textNodes = first.locator(".node-rule-candidates"), warnings = first.locator("details.import-warnings");
    assert.equal(await textNodes.evaluate(node => node.open), false, "Text node recognition starts collapsed");
    assert.equal(await warnings.evaluate(node => node.open), false, "Import warnings start collapsed");
    assert.equal(await warnings.locator(":scope > summary").textContent(), `需注意 ${await warnings.locator("li").count()}`);
    assert.equal(await warnings.locator("li").first().isVisible(), false);
    await warnings.locator(":scope > summary").click();
    await warnings.locator("li").first().waitFor();
    await warnings.locator(":scope > summary").click();
    await page.screenshot({ path: path.join(output, `${engine}-${width}-collapsed.png`), fullPage: true });
    await textNodes.locator(":scope > summary").click();
    assert.equal(await first.locator('[data-import-field="prompt"]').inputValue(), "");
    await second.locator('[data-import-field="prompt"]').fill("keep my manual prompt");
    await first.locator('[data-node-rule-id="10"]').click();
    await frame.locator("#nodeRuleOperation").waitFor({ state: "attached" });
    assert.equal(await frame.locator("#nodeRuleSave").isDisabled(), true);
    assert.equal(await frame.locator("#nodeRulePreviewButton").isDisabled(), true);
    assert.match(await frame.locator(".node-rule-ports").textContent(), /optional_text/);
    assert.match(await frame.locator(".node-rule-ports").textContent(), /unused_text/);
    assert.match(await frame.locator(".node-rule-ports").textContent(), /未连接/);
    assert.equal(await frame.locator('#nodeRuleScope option[value="type"]').evaluate((option) => option.disabled), true);
    const material = await frame.locator("#studioModal").evaluate((node) => ({ blur: getComputedStyle(node).backdropFilter || getComputedStyle(node).webkitBackdropFilter, width: node.getBoundingClientRect().width, viewport: innerWidth, page: document.documentElement.scrollWidth }));
    assert.match(material.blur, /blur/);
    assert.ok(material.width <= material.viewport && material.page <= material.viewport + 1, JSON.stringify(material));
    await choose(frame, "#nodeRuleOperation", "concat");
    await choose(frame, '[data-node-rule-input="0"]', "text_a");
    await frame.locator("#nodeRuleAddInput").click();
    await choose(frame, '[data-node-rule-input="1"]', "text_b");
    await choose(frame, "#nodeRuleOutput", "0");
    await page.screenshot({ path: path.join(output, `${engine}-${width}-binding.png`), fullPage: true });
    // Editing during an in-flight response must never enable saving stale data.
    let releasePreview;
    const heldPreview = new Promise((resolve) => { releasePreview = resolve; });
    let previewReceived = false;
    const previewRoute = async (route) => { const response = await route.fetch(); previewReceived = true; await heldPreview; await route.fulfill({ response }); };
    await page.route("**/imports/node-rules/preview", previewRoute);
    await frame.locator("#nodeRulePreviewButton").click();
    await until(() => previewReceived, "Preview request did not arrive");
    await frame.locator("#studioModalClose").click();
    assert.equal(await frame.locator("#studioModalRoot").isVisible(), true, "A pending modal action keeps its lifecycle intact");
    await frame.locator("#nodeRuleDelimiter").evaluate((field) => { field.value = " | "; field.dispatchEvent(new Event("input", { bubbles: true })); });
    releasePreview();
    await frame.locator("#studioModal:not([aria-busy])").waitFor();
    await page.unroute("**/imports/node-rules/preview", previewRoute);
    assert.equal(await frame.locator("#nodeRuleSave").isDisabled(), true, "Late preview of a changed draft cannot enable save");
    await frame.locator("#nodeRuleDelimiter").fill(", ");
    await frame.locator("#nodeRulePreviewButton").click();
    await frame.locator("#nodeRuleSave:not(:disabled)").waitFor();
    assert.match(await frame.locator("#nodeRulePreview").textContent(), /landscape-0, daylight/);
    assert.match(await frame.locator("#nodeRulePreview").textContent(), /按用户规则识别/);
    await page.screenshot({ path: path.join(output, `${engine}-${width}-preview.png`), fullPage: true });
    await frame.locator("#nodeRuleDelimiter").fill(" | ");
    assert.equal(await frame.locator("#nodeRuleSave").isDisabled(), true, "Any edit invalidates preview");
    await frame.locator("#nodeRuleDelimiter").fill(", ");
    await frame.locator("#nodeRulePreviewButton").click();
    await frame.locator("#nodeRuleSave:not(:disabled)").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await until(() => first.locator('[data-import-field="prompt"]').inputValue().then((text) => text === "landscape-0, daylight"), "Saved rule did not reparse imports");
    assert.equal(await second.locator('[data-import-field="prompt"]').inputValue(), "keep my manual prompt");
    assert.match(await first.locator('[data-node-rule-id="10"]').textContent(), /已绑定用户规则/);
    assert.match(await first.locator('[data-import-field="prompt"]').locator("..").textContent(), /按用户规则识别/);
    // Reopen an existing binding, then cancel: the stored rule and drafts survive.
    await first.locator('[data-node-rule-id="10"]').click();
    await frame.locator("#nodeRuleOperation").waitFor({ state: "attached" });
    assert.equal(await frame.locator("#nodeRuleOperation").inputValue(), "concat");
    await frame.locator("#studioModalClose").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator("#importNodeRulesButton").click();
    const row = frame.locator(".node-rule-list-item").filter({ hasText: `ManualTextJoin-${marker}` });
    await row.locator("button").click();
    assert.equal(await row.locator("button").textContent(), "确认删除");
    await row.locator("button").click();
    await row.waitFor({ state: "detached" });
    await frame.locator("#studioModalClose").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal(await first.locator('[data-import-field="prompt"]').inputValue(), "");
    assert.equal(await second.locator('[data-import-field="prompt"]').inputValue(), "keep my manual prompt");

    // A custom observer binds its saved widget, without assigning node polarity.
    await first.locator('[data-node-rule-id="11"]').click();
    await frame.locator("#nodeRuleOperation").waitFor({ state: "attached" });
    await choose(frame, "#nodeRuleOperation", "observer");
    await choose(frame, '[data-node-rule-input="0"]', "text");
    await choose(frame, "#nodeRuleWidget", "0");
    assert.equal(await frame.locator("#nodeRuleOutput").count(), 0);
    await frame.locator("#nodeRulePreviewButton").click();
    await frame.locator("#nodeRuleSave:not(:disabled)").waitFor();
    assert.match(await frame.locator("#nodeRulePreview").textContent(), /landscape-0, daylight/);
    await frame.locator("#nodeRuleSave").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    assert.equal(await first.locator('[data-import-field="prompt"]').inputValue(), "landscape-0, daylight");
    assert.equal(await second.locator('[data-import-field="prompt"]').inputValue(), "keep my manual prompt");
    await frame.locator("#cancelImportButton").click();
    assert.equal(await rulesButton.isVisible(), false, "Cancelling the batch hides ComfyUI actions");
    await frame.locator("#importFiles").setInputFiles(files[0]);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    await frame.locator("#importNodeRulesButton").click();
    await frame.locator(".node-rule-list-item").filter({ hasText: `ManualTextDisplay-${marker}` }).waitFor();
    assert.equal(await frame.locator(".node-rule-list-item").filter({ hasText: `ManualTextDisplay-${marker}` }).count(), 1, "Cancel import must preserve independently saved rules");
    await page.screenshot({ path: path.join(output, `${engine}-${width}.png`), fullPage: true });
    await frame.locator("#studioModalClose").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    await frame.locator("#importFiles").setInputFiles(plain);
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    await first.locator("[data-remove-import]").click();
    assert.equal(await frame.locator("#importGrid .import-card").count(), 1);
    assert.equal(await rulesButton.isVisible(), false, "Removing the last ComfyUI image hides the button even when plain images remain");
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: manual binding, glass/ports, preview invalidation, rule persistence/delete, batch reparse, manual protection and custom observer passed`);
  } finally { await page.close(); }
}
(async () => {
  for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) await run(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Artifacts: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
