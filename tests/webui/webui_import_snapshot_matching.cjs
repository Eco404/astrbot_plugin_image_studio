/* Synthetic PNGs only; run against the isolated tests/support/webui_harness.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const root = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio/`;
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-snapshot-matching-"));
const kinds = ["source", "disconnected", "rewired", "producer-port", "downstream-port", "downstream-type", "save", "observer", "api-fallback", "conflict"];
const matching = kinds.slice(0, 3);
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);marker=sys.argv[2];files={}
for index,kind in enumerate(json.loads(sys.argv[3])):
 port=1 if kind=="producer-port" else 0
 save="189" if kind=="save" else "188"
 observers=("145",) if kind=="observer" else ("143","144")
 graph={
  "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"landscape-model.safetensors"}},
  "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur, watermark"}},
  "6":{"class_type":"EmptyLatentImage","inputs":{"width":320,"height":240,"batch_size":1}},
  "234":{"class_type":"TextInput","inputs":{"text":f"upstream-static-{kind}"}},
  "235":{"class_type":"TextInput","inputs":{"text":"optional landscape description"}},
  "236":{"class_type":"TextInput","inputs":{"text":"alternative landscape description"}},
  "231":{"class_type":"CustomPromptJoin","inputs":{"text_a":["234",0],"text_b":["236" if kind=="rewired" else "235",0]}},
  "137":{"class_type":"CustomPromptAssembler","inputs":{"text":["231",0]}},
  "138":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["137",port]}},
  "19":{"class_type":"KSamplerAdvanced" if kind=="downstream-type" else "KSampler","inputs":{"model":["1",0],"positive":["138",0],"negative":["4",0],"latent_image":["6",0],"seed":42+index,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
  "20":{"class_type":"VAEDecode","inputs":{"samples":["19",1 if kind=="downstream-port" else 0],"vae":["1",2]}},
  save:{"class_type":"SaveImage","inputs":{"images":["20",0],"filename_prefix":f"landscape-{kind}"}}
 }
 if kind=="disconnected": graph["231"]["inputs"].pop("text_b")
 for observer in observers:
  graph[observer]={"class_type":"easy showAnything","inputs":{"anything":["137",port],"text":f"api-rendered-{kind}"}}
 nodes=[];links=[]
 for identifier,node in graph.items():
  inputs=[]
  for field,value in node["inputs"].items():
   if isinstance(value,list):
    link_id=len(links)+1;links.append([link_id,int(value[0]),value[1],int(identifier),len(inputs),"STRING"])
    inputs.append({"name":field,"type":"STRING","link":link_id})
  widgets=[]
  if identifier in observers and kind!="api-fallback":
   widgets=["conflicting-other-snapshot" if kind=="conflict" and identifier=="144" else f"rendered-{kind}"]
  nodes.append({"id":int(identifier),"type":node["class_type"],"inputs":inputs,"widgets_values":widgets,"pos":[0,index*10],"size":[200,100]})
 workflow={"nodes":nodes,"links":links,"groups":[],"version":0.4,"extra":{"marker":marker,"kind":kind}}
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",json.dumps(graph));metadata.add_text("workflow",json.dumps(workflow))
 image=Image.new("RGB",(320,240),(175+index,204,210));draw=ImageDraw.Draw(image);draw.rectangle((0,155,320,240),fill=(105,150,135));draw.polygon([(0,160),(120,40),(250,160)],fill=(120,142,151))
 target=folder/f"{marker}-{kind}.png";image.save(target,pnginfo=metadata);files[kind]=str(target)
print(json.dumps(files))
`;

async function waitUntil(check, message) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(message);
}
async function get(page, endpoint) {
  const response = await page.request.get(root + endpoint);
  assert.ok(response.ok(), await response.text());
  return response.json();
}
async function reveal(card) {
  await card.evaluate((node) => node.scrollIntoView({ block: "start" }));
  await card.locator('[data-import-field="prompt"]').waitFor({ state: "attached" });
}
async function snapshotButton(card, text) {
  await reveal(card);
  const details = card.locator(".import-prompt-candidates");
  if (!await details.evaluate((node) => node.open)) await details.locator(":scope > summary").click();
  await card.locator(".prompt-candidate").first().waitFor({ state: "attached" });
  const id = await card.locator(".prompt-candidate").evaluateAll((nodes, expected) => nodes.find((node) => node.querySelector(".prompt-candidate-text > pre")?.textContent === expected)?.dataset.promptCandidate, text);
  assert.ok(id, `Missing own display snapshot ${text}: ${await details.textContent()}`);
  return card.locator(`[data-prompt-candidate="${id}"] [data-batch-candidate-target="prompt"]`);
}

async function verify(browser, engine, width) {
  const marker = `${path.basename(output)}-${engine}-${width}`;
  const files = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, marker, JSON.stringify(kinds)], { encoding: "utf8" }));
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(20000);
  const errors = [], inspections = new Map();
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", async (response) => {
    if (!response.url().endsWith("/imports/inspect") || !response.ok()) return;
    try {
      const metadata = response.request().postDataJSON()?.metadata;
      const kind = JSON.parse(metadata?.workflow || "{}").extra?.kind;
      if (kind) { const body = await response.json(); inspections.set(kind, (body.data || body).normalized); }
    } catch (error) { errors.push(error.message); }
  });
  try {
    await page.goto(base);
    const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="import"]').click();
    await frame.locator("#importFiles").setInputFiles(kinds.map((kind) => files[kind]));
    await waitUntil(() => inspections.size === kinds.length, "All synthetic PNGs must be inspected");
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    const cardIn = (grid, kind) => grid.locator(".import-card").filter({ has: frame.locator(`.import-card-header strong[data-tooltip="${path.basename(files[kind])}"]`) });
    const draft = frame.locator("#importGrid");
    const snapshots = (kind) => inspections.get(kind).prompt_candidates.filter((candidate) => candidate.status === "display_snapshot");
    assert.equal(snapshots("source").length, 1, "Identical observer values merge without ambiguity");
    assert.equal(snapshots("conflict").length, 2, "Different observer values retain ambiguity");
    const source = snapshots("source")[0];
    for (const kind of matching) assert.equal(snapshots(kind)[0].match_key, source.match_key, `${kind}: upstream rewiring must not invalidate a display snapshot`);
    for (const kind of ["producer-port", "downstream-port", "downstream-type", "save"]) assert.notEqual(snapshots(kind)[0].match_key, source.match_key, `${kind}: unsafe downstream differences must still be rejected`);
    for (const kind of ["observer", "api-fallback"]) assert.equal(snapshots(kind)[0].match_key, source.match_key, `${kind}: frontend provenance guard, not topology, must reject this match`);
    const saveKey = (kind) => inspections.get(kind).outputs.find((item) => item.kind === "save").match_key;
    const staticKey = (kind) => inspections.get(kind).prompt_candidates.find((item) => item.node_id === "234" && item.status !== "display_snapshot")?.match_key;
    assert.ok(staticKey("source"), "Static upstream candidate remains available independently of the snapshot");
    for (const kind of matching.slice(1)) {
      assert.notEqual(saveKey(kind), saveKey("source"), "Batch save-output matching retains the full branch guard");
      assert.notEqual(staticKey(kind), staticKey("source"), "Static candidate matching retains the full branch guard");
    }
    await (await snapshotButton(cardIn(draft, "source"), "rendered-source")).click();
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    for (const kind of kinds) {
      const card = cardIn(draft, kind); await reveal(card);
      assert.equal(await card.locator('[data-import-field="prompt"]').inputValue(), matching.includes(kind) ? `rendered-${kind}` : "", `${kind}: batch choice must use only that image's own safe snapshot`);
    }
    assert.match(await frame.locator("#importBatchSummary").textContent(), /已应用 3 张/);
    assert.match(await frame.locator("#importBatchSummary").textContent(), /节点不匹配 6 张/);
    assert.match(await frame.locator("#importBatchSummary").textContent(), /快照冲突 1 张/);
    await (await snapshotButton(cardIn(draft, "source"), "rendered-source")).click();
    await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
    assert.match(await frame.locator("#importBatchSummary").textContent(), /已填入或已选择 3 张/, "Reapplying includes the source and cannot append duplicate snapshots");

    // Persist an imported group with neutral prompts, then exercise the same
    // bulk action through the lazy editor and verify its saved per-image values.
    for (const kind of kinds) {
      const card = cardIn(draft, kind); await reveal(card);
      await card.locator('[data-import-field="prompt"]').fill(marker);
    }
    await frame.locator("#importGroupOption .toggle-control").click();
    await frame.locator("#confirmImportButton").click();
    await waitUntil(() => draft.locator(".import-card").count().then((count) => count === 0), "Synthetic group import did not finish");
    const listing = await get(page, "gallery/list?source=import&query=" + encodeURIComponent(marker));
    assert.equal(listing.total, 1);
    const id = listing.items[0].id;
    await frame.locator('[data-view="gallery"]').click();
    await frame.locator("#gallerySearch").fill(marker);
    await frame.locator("#gallerySearch").press("Enter");
    await frame.locator("#galleryRefresh").click();
    await frame.locator(`[data-gallery-id="${id}"] .gallery-info`).click();
    await frame.locator("#detailImportEdit:not(:disabled)").click();
    const editor = frame.locator("#importEditGrid");
    await editor.locator(".import-card").nth(kinds.length - 1).waitFor({ state: "attached" });
    await (await snapshotButton(cardIn(editor, "source"), "rendered-source")).click();
    await frame.locator("#importEditSave:not(:disabled)").waitFor();
    assert.match(await frame.locator("#importEditBatchSummary").textContent(), /已应用 3 张/);
    assert.match(await frame.locator("#importEditBatchSummary").textContent(), /节点不匹配 6 张/);
    assert.match(await frame.locator("#importEditBatchSummary").textContent(), /快照冲突 1 张/);
    for (const kind of kinds) {
      const card = cardIn(editor, kind); await reveal(card);
      const expected = matching.includes(kind) ? `${marker}\n\nrendered-${kind}` : marker;
      assert.equal(await card.locator('[data-import-field="prompt"]').inputValue(), expected, `${kind}: editor must use the same safe own-text matching`);
    }
    const bounds = await editor.evaluate((node) => ({ page: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth, grid: node.scrollWidth, width: node.clientWidth }));
    assert.ok(bounds.page <= bounds.viewport + 1 && bounds.grid <= bounds.width + 1, JSON.stringify(bounds));
    await frame.locator("#importEditSave").click();
    await frame.locator("#studioModalRoot").waitFor({ state: "hidden" });
    const saved = await get(page, `gallery/import-edit/${id}`);
    for (const kind of kinds) {
      const item = saved.items.find((entry) => entry.filename === path.basename(files[kind]));
      assert.ok(item);
      assert.equal(item.fields.prompt, matching.includes(kind) ? `${marker}\n\nrendered-${kind}` : marker, `${kind}: own snapshot must survive editor persistence`);
    }
    assert.deepEqual(errors, []);
    console.log(`${engine}-${width}: upstream-independent snapshots, downstream/provenance guards, strict static/save matching, own-image application and imported-group editing passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const engine of process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"]) {
    if (!["chromium", "webkit"].includes(engine)) throw new Error(`Unsupported STUDIO_BROWSER: ${engine}`);
    const browser = await playwright[engine].launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, engine, width); }
    finally { await browser.close(); }
  }
  console.log(`Synthetic snapshot fixtures: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
