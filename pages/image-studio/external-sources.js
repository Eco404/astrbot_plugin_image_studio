(function () {
  "use strict";
  window.ImageStudioExternalSources = function (hooks) {
    const { state, apiGet, apiPost, escape, formatBytes, formatDate, showNotice, errorMessage } = hooks;
    const $ = id => document.getElementById(id), copy = value => JSON.parse(JSON.stringify(value));
    const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
    const sources = new Map(), scanRequests = new Set();
    let types = [{ id: "nai", name: "NAI 插件图库" }, { id: "directory", name: "自定义目录" }];
    let drafts = {}, saved = {}, editor = null;
    let timer = 0, pending = null, disposed = false, revision = 0, refreshAgain = false;
    function normalize(value = {}, id = "") {
      const type = value.type || (id === "nai" ? "nai" : "directory");
      return { type, name: value.name || (type === "nai" ? "NAI 插件图库" : "自定义图库"), enabled: !!value.enabled, path: value.path || "", recursive: !!value.recursive, permissions: { favorite: true, delete: type === "nai", download: true, reference: true, ...value.permissions } };
    }
    function normalizeMap(value) { return Object.fromEntries(Object.entries(value || {}).map(([id, item]) => [id, normalize(item, id)])); }
    function dirty(id) { return !equal(drafts[id], saved[id]); }
    function isVisible() { return !disposed && state.view === "settings" && !document.hidden; }
    function running(source) { return ["enumerating", "scanning"].includes(source?.status); }
    function statusText(source) {
      if (!source.enabled) return "已停用";
      if (source.status === "enumerating") return "正在枚举图片文件…";
      if (source.status === "scanning") return `正在扫描：${Number(source.processed || 0)} 张 / ${Number(source.total || 0)} 张`;
      if (source.status === "complete") return `已完成：共 ${Number(source.indexed_count || 0)} 张`;
      if (source.status === "warning") return `扫描完成：共 ${Number(source.indexed_count || 0)} 张，部分文件需检查`;
      if (["unavailable", "error"].includes(source.status)) return source.status === "error" ? "扫描失败" : "来源暂不可用";
      return "等待扫描";
    }
    function capsule(id, value) {
      if (dirty(id)) return ["pending", "待保存"];
      if (!value.enabled) return ["disabled", "已停用"];
      const source = sources.get(id);
      if (running(source)) return ["scanning", "扫描中"];
      if (source?.status === "complete") return ["complete", "正常"];
      if (source?.status === "warning") return ["warning", "部分异常"];
      if (["unavailable", "error"].includes(source?.status)) return ["error", "不可用"];
      return ["waiting", "等待扫描"];
    }
    function canScan(id, value) { return !!saved[id]?.enabled && !!value?.enabled && !dirty(id) && !running(sources.get(id)) && !scanRequests.has(id); }
    function render() {
      const root = $("externalSourcesList"); if (!root) return;
      const ids = new Set(Object.keys(drafts));
      for (const element of Array.from(root.children)) if (!ids.has(element.dataset.externalSource)) element.remove();
      for (const [id, value] of Object.entries(drafts)) {
        let row = Array.from(root.children).find(element => element.dataset.externalSource === id);
        if (!row) {
          row = document.createElement("button"); row.type = "button"; row.className = "external-source-entry"; row.dataset.externalSource = id;
          row.innerHTML = '<span data-external-name></span><span class="external-source-status" data-external-status></span>';
          row.addEventListener("click", () => void edit(id)); root.appendChild(row);
        }
        const [tone, label] = capsule(id, value);
        row.querySelector("[data-external-name]").textContent = value.name;
        const status = row.querySelector("[data-external-status]"); status.textContent = label; status.dataset.status = tone;
        row.setAttribute("aria-label", `${value.name}，${label}，编辑图库`);
      }
      $("externalSourcesEmpty").hidden = !!ids.size;
      $("addExternalSource").disabled = !state.settings;
      renderEditorStatus();
    }
    function renderHealth() {
      const root = $("storageExternalSources"); if (!root) return;
      const enabled = Object.entries(saved).filter(([, value]) => value.enabled).map(([id, value]) => ({ id, ...value, ...sources.get(id), name: value.name, enabled: value.enabled }));
      root.classList.toggle("is-hidden", !enabled.length);
      root.innerHTML = enabled.map(source => `<div class="storage-external-source"><h3>${escape(source.name)}</h3><div class="storage-health-grid"><div><span>外部原图</span><strong>${Number(source.indexed_count || 0)} 张 · ${formatBytes(source.size_bytes || 0)}</strong></div><div><span>本地预览</span><strong>${Number(source.thumbnail_count || 0)} 张 · ${formatBytes(source.thumbnail_bytes || 0)}</strong></div></div><p class="field-hint">${escape(statusText(source))} · 原图不计入本插件历史限额</p></div>`).join("");
    }
    function renderEditorStatus() {
      if (!editor || !$("externalEditorStatus")) return;
      const { id } = editor, source = { ...saved[id], ...sources.get(id), enabled: !!saved[id]?.enabled };
      $("externalEditorStatus").textContent = !saved[id] ? "保存全部设置后开始扫描。" : dirty(id) ? `${statusText(source)} · 此条目有待保存设置` : statusText(source);
      $("externalEditorLast").textContent = source.last_scan_at ? `最近扫描：${formatDate(source.last_scan_at)}` : "尚未完成扫描";
      $("externalEditorOriginals").textContent = `${Number(source.indexed_count || 0)} 张 · ${formatBytes(source.size_bytes || 0)}`;
      $("externalEditorPreviews").textContent = `${Number(source.thumbnail_count || 0)} 张 · ${formatBytes(source.thumbnail_bytes || 0)}`;
      const errors = [source.error, ...(source.errors || []).map(error => typeof error === "string" ? error : `${error.filename || error.path || ""}${error.filename || error.path ? "：" : ""}${error.message || "文件处理失败"}`)].filter(Boolean);
      $("externalEditorErrors").textContent = Array.from(new Set(errors)).join("\n");
      const candidate = readEditor(false);
      $("externalEditorScan").disabled = !canScan(id, drafts[id]) || !equal(candidate, drafts[id]);
      if (candidate.type === "nai") $("externalEditorPath").value = types.find(type => type.id === "nai")?.path || sources.get(id)?.path || "自动定位 NAI 插件图库";
    }
    function ingest(items, observe = true, replace = false) {
      let changed = false;
      if (replace) for (const id of sources.keys()) if (!(items || []).some(source => source.id === id)) sources.delete(id);
      for (const source of items || []) {
        if (!source?.id) continue;
        const previous = sources.get(source.id);
        if (previous && (previous.enabled !== source.enabled || previous.last_scan_at !== source.last_scan_at || previous.indexed_count !== source.indexed_count || running(previous) && !running(source))) changed = true;
        sources.set(source.id, { ...previous, ...source });
      }
      render(); renderHealth(); if (observe && changed) hooks.invalidateBrowseCache();
    }
    function schedule() {
      window.clearTimeout(timer); timer = 0;
      if (!isVisible() || !Object.values(saved).some(source => source.enabled)) return;
      timer = window.setTimeout(() => void refresh(), Array.from(sources.values()).some(running) ? 1500 : 10000);
    }
    async function refresh() {
      window.clearTimeout(timer); timer = 0; if (!isVisible()) return;
      if (pending) { refreshAgain = true; return pending; }
      const requestedRevision = revision;
      pending = (async () => {
        try {
          const payload = await apiGet("external/status"); if (requestedRevision !== revision) return;
          if (Array.isArray(payload.types) && payload.types.length) types = payload.types;
          ingest(payload.sources, true, true); $("externalSourcesError").textContent = "";
        } catch (error) { if (isVisible() && requestedRevision === revision) $("externalSourcesError").textContent = errorMessage(error, "扫描状态读取失败"); }
      })().finally(() => {
        pending = null;
        if (refreshAgain) { refreshAgain = false; if (isVisible()) { void refresh(); return; } }
        schedule();
      }); return pending;
    }
    async function scan(id) {
      if (!canScan(id, drafts[id])) return;
      scanRequests.add(id); render();
      try { await apiPost("external/scan", { source_id: id }); await refresh(); }
      catch (error) { showNotice(errorMessage(error, "启动扫描失败"), "error"); }
      finally { scanRequests.delete(id); render(); }
    }
    function readEditor(validate = true) {
      const type = $("externalEditorType").value;
      const value = { type, name: $("externalEditorName").value.trim(), enabled: $("externalEditorEnabled").checked, path: type === "nai" ? "" : $("externalEditorPath").value.trim(), recursive: $("externalEditorRecursive").checked, permissions: {} };
      for (const action of ["favorite", "delete", "download", "reference"]) value.permissions[action] = $("externalPermission-" + action).checked;
      if (validate) {
        if (!value.name) throw new Error("请填写图库名称。");
        if (value.name.length > 80) throw new Error("图库名称不能超过 80 个字符。");
        if (type === "directory" && !value.path.startsWith("/")) throw new Error("请填写 AstrBot 容器内的绝对路径，例如 /data/pictures。");
      } return value;
    }
    function updateEditorType(reset = false) {
      const builtin = $("externalEditorType").value === "nai";
      $("externalEditorPath").readOnly = builtin;
      $("externalEditorPathHint").textContent = builtin ? "自动定位 NAI 插件保存历史图片的目录。" : "填写 AstrBot 所在容器内的目录；没有生成参数的图片也会正常加入画廊。";
      if (reset) { $("externalEditorName").value = builtin ? "NAI 插件图库" : "自定义图库"; $("externalEditorPath").value = ""; $("externalPermission-delete").checked = builtin; }
      renderEditorStatus();
    }
    async function edit(id = "", candidate = null) {
      const existing = !!id, initial = copy(candidate || drafts[id] || normalize({ type: "nai", enabled: true }));
      const toggle = (key, label, checked) => `<div class="toggle-row"><label for="${key}">${label}</label><label class="toggle-control"><input id="${key}" type="checkbox" ${checked ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
      editor = { id };
      const body = `<div class="external-source-editor"><label class="field">图库类型<select id="externalEditorType">${types.map(type => `<option value="${escape(type.id)}" ${type.id === initial.type ? "selected" : ""}>${escape(type.name)}</option>`).join("")}</select></label><label class="field">名称<input id="externalEditorName" maxlength="80" value="${escape(initial.name)}" /></label><label class="field">图片目录<input id="externalEditorPath" value="${escape(initial.path)}" placeholder="/data/pictures" spellcheck="false" /><span class="field-hint" id="externalEditorPathHint"></span></label>${toggle("externalEditorEnabled", "启用图库", initial.enabled)}${toggle("externalEditorRecursive", "扫描子目录", initial.recursive)}<div class="external-permissions"><h3>允许的操作</h3>${[["favorite", "收藏"], ["delete", "删除原图"], ["download", "下载 / 导出原图"], ["reference", "用作参考图"]].map(([action, label]) => toggle("externalPermission-" + action, label, initial.permissions[action])).join("")}<p class="field-hint">收藏不会阻止来源清理原图；关闭收藏权限后保留已有收藏状态。</p></div><div class="external-editor-health"><div class="external-source-status-row"><span id="externalEditorStatus" role="status"></span><button class="quiet-button" id="externalEditorScan" type="button">重新扫描</button></div><div class="storage-health-grid"><div><span>外部原图</span><strong id="externalEditorOriginals"></strong></div><div><span>本地预览</span><strong id="externalEditorPreviews"></strong></div></div><p class="field-hint" id="externalEditorLast"></p><p class="inline-error" id="externalEditorErrors"></p></div><p class="field-hint">确认后将更新设置草稿，点击“保存全部设置”后生效。</p></div>`;
      const result = await hooks.openModal(existing ? "编辑外部图库" : "添加外部图库", body, [
        ...(existing ? [{ label: "移除图库", danger: true, id: "externalEditorRemove", action: () => ({ remove: true }) }] : []),
        { label: "取消", action: () => false }, { label: "确认", primary: true, id: "externalEditorApply", action: () => ({ value: readEditor() }) },
      ], { externalEditor: true, focus: "externalEditorName", onOpen: () => {
        $("externalEditorType").addEventListener("change", () => updateEditorType(true));
        $("externalEditorScan").addEventListener("click", () => void scan(id));
        $("studioModalBody").querySelectorAll("input, select").forEach(input => input.addEventListener("input", renderEditorStatus));
        updateEditorType();
      }, onClose: () => { editor = null; } });
      if (!result) return;
      if (result.remove) {
        const accepted = await hooks.openModal("移除图库", `<p>移除“${escape(initial.name)}”？</p><p>保存全部设置后，将移除此外部图库的登记信息、收藏状态及不再使用的预览。来源中的原文件和已经独立保存的本地图片、参考图会保留。</p>`, [{ label: "取消", action: () => false }, { label: "确认移除", danger: true, action: () => true }]);
        if (!accepted) { void edit(id, initial); return; }
        delete drafts[id];
      } else {
        const value = result.value, previous = saved[id];
        if (previous && (previous.type !== value.type || previous.path !== value.path || previous.recursive !== value.recursive)) {
          const accepted = await hooks.openModal("重建图库索引", "<p>更改图库类型、目录或扫描范围后，保存设置时将重建此图库索引，并清除旧的外部收藏状态。来源原文件及独立保存的本地图片、参考图会保留。</p>", [{ label: "返回编辑", action: () => false }, { label: "确认更改", primary: true, action: () => true }]);
          if (!accepted) { void edit(id, value); return; }
        }
        id ||= `source_${crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : Date.now().toString(36) + Math.random().toString(36).slice(2)}`;
        drafts[id] = value;
      }
      render(); hooks.updateSettingsDirty();
    }
    function settingsLoaded(preserveDraft = false, submittedSources = null) {
      revision++;
      const incoming = normalizeMap(state.settings?.webui.external_sources);
      if (preserveDraft && submittedSources) {
        const submitted = normalizeMap(submittedSources), next = {};
        for (const id of new Set([...Object.keys(incoming), ...Object.keys(drafts), ...Object.keys(submitted)])) {
          const value = equal(drafts[id], submitted[id]) ? incoming[id] : drafts[id]; if (value) next[id] = copy(value);
        } drafts = next;
      } else if (!preserveDraft) drafts = copy(incoming);
      saved = incoming;
      for (const id of sources.keys()) if (!saved[id]) sources.delete(id);
      render(); renderHealth(); void refresh();
    }
    function settingsDraft(webui) { webui.external_sources = copy(drafts); }
    function viewChanged() { if (isVisible()) void refresh(); else { window.clearTimeout(timer); timer = 0; } }
    $("addExternalSource").addEventListener("click", () => void edit());
    document.addEventListener("visibilitychange", viewChanged);
    window.addEventListener("pagehide", () => { disposed = true; window.clearTimeout(timer); });
    window.addEventListener("pageshow", () => { disposed = false; viewChanged(); });
    render(); return { settingsLoaded, settingsDraft, viewChanged, refresh, ingest };
  };
})();
