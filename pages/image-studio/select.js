(function () {
  "use strict";

  const controls = new Map();
  let sequence = 0;
  let opened = null;
  let frame = 0;
  let pendingRefresh = false;
  let typeahead = "";
  let typeaheadAt = 0;
  let lastTouchY = 0;
  const ATTRIBUTE_NAMES = ["disabled", "hidden", "class", "style", "selected", "multiple", "data-all-label", "label", "value", "required", "name", "data-tooltip", "aria-label", "aria-labelledby", "aria-describedby", "tabindex", "list"];
  const SOURCE_SELECTOR = "select, input[list], input[data-studio-datalist]";
  const GALLERY_SELECT_IDS = new Set(["galleryProvider", "galleryMode", "gallerySource", "galleryEngine"]);

  function getGalleryDefault(id) {
    if (!GALLERY_SELECT_IDS.has(id)) return null;
    return window.ImageStudioGalleryPreferences?.getFilter(id) || null;
  }

  async function saveGalleryDefault(control) {
    if (!control.multiple || !GALLERY_SELECT_IDS.has(control.select.id) || control.savingDefault) return;
    const options = enabledOptions(control);
    const selected = options.filter((option) => option.selected);
    if (!selected.length) return;
    const selection = selected.length === options.length ? { mode: "all" } : { mode: "values", values: selected.map((option) => option.value) };
    const currentSelection = () => JSON.stringify(Array.from(control.select.options, option => [option.value, option.selected]));
    const expected = currentSelection();
    control.savingDefault = true;
    renderMenu(control);
    try {
      if (!window.ImageStudioGalleryPreferences) throw new Error("浏览器显示设置组件尚未加载，请刷新页面重试。");
      await window.ImageStudioGalleryPreferences.setFilter(control.select.id, selection);
      window.dispatchEvent(new CustomEvent("image-studio-gallery-default-saved", { detail: { id: control.select.id, selection, selectionUnchanged: expected === currentSelection() } }));
    } catch (error) {
      window.dispatchEvent(new CustomEvent("image-studio-gallery-default-error", { detail: { id: control.select.id, message: `无法保存筛选默认值：${error?.message || "请检查浏览器存储权限。"}` } }));
    } finally {
      control.savingDefault = false;
      renderMenu(control);
    }
  }

  function checkIcon() {
    const icons = window.StudioIcons;
    if (!icons?.Check) return "";
    return icons.createElement(icons.Check, { width: 16, height: 16, "aria-hidden": "true", "stroke-width": 2 }).outerHTML;
  }

  function controlName(select) {
    const explicit = select.getAttribute("aria-label");
    if (explicit) return explicit;
    const labelled = select.getAttribute("aria-labelledby");
    if (labelled) {
      const result = labelled.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "").join(" ").trim();
      if (result) return result;
    }
    const labels = Array.from(select.labels || []);
    if (!labels.length) {
      const parentLabel = select.closest(".field, .model-select-row")?.querySelector("label");
      if (parentLabel) labels.push(parentLabel);
    }
    const result = labels.map((label) => {
      const copy = label.cloneNode(true);
      copy.querySelectorAll("select, button, .studio-select, svg").forEach((element) => element.remove());
      return copy.textContent.trim();
    }).filter(Boolean).join(" ");
    return result || select.dataset.tooltip || select.name || "选择选项";
  }

  function optionData(select) {
    const control = controls.get(select);
    const editable = !!control?.editable;
    const query = editable && !control.showAll ? select.value.trim().toLocaleLowerCase() : "";
    const source = editable ? document.getElementById(control.listId) : select;
    return Array.from(source?.options || []).map((option, index) => ({
      index,
      value: option.value,
      label: option.label || option.textContent || option.value || "",
      selected: !!option.selected,
      disabled: option.disabled || !!option.closest("optgroup")?.disabled,
      hidden: option.hidden || option.style.display === "none" || !!option.closest("optgroup")?.hidden || !!query && !`${option.value} ${option.label || option.textContent}`.toLocaleLowerCase().includes(query),
      group: option.parentElement?.tagName === "OPTGROUP" ? option.parentElement.label : "",
    }));
  }

  function selectedIndex(control) { return control.editable ? control.options.find((option) => option.value === control.select.value)?.index ?? -1 : control.select.selectedIndex; }

  function isVisible(control) {
    if (!control.select.isConnected || !control.wrapper.isConnected || control.wrapper.hidden) return false;
    if (!control.trigger.getClientRects().length) return false;
    const style = getComputedStyle(control.trigger);
    return style.visibility !== "hidden" && !control.trigger.closest('[inert], [aria-hidden="true"], .is-hidden');
  }

  function update(control) {
    if (control.editable) { updateEditable(control); return; }
    const { select, trigger, wrapper } = control;
    const options = optionData(select);
    const name = controlName(select);
    const disabled = select.matches(":disabled");
    const hidden = select.hidden || select.classList.contains("is-hidden") || select.style.display === "none" || select.style.visibility === "hidden";
    const classes = Array.from(select.classList).filter((name) => !["studio-select-native", "is-hidden"].includes(name)).join(" ");
    const fingerprint = JSON.stringify([select.selectedIndex, options, select.multiple, select.dataset.allLabel, name, disabled, hidden, classes, select.required, select.getAttribute("aria-describedby"), select.dataset.tooltip]);
    if (fingerprint === control.fingerprint) {
      if (opened === control && (!isVisible(control) || disabled)) close();
      return;
    }
    control.fingerprint = fingerprint;
    control.options = options;
    control.multiple = select.multiple;
    trigger.disabled = disabled;
    wrapper.hidden = hidden;
    wrapper.className = `studio-select${classes ? ` ${classes}` : ""}${opened === control ? " is-open" : ""}`;
    wrapper.dataset.selectId = select.id || "";
    trigger.dataset.selectId = select.id || "";
    trigger.setAttribute("aria-label", name);
    trigger.setAttribute("aria-required", String(select.required));
    trigger.setAttribute("aria-disabled", String(disabled));
    const description = select.getAttribute("aria-describedby");
    if (description) trigger.setAttribute("aria-describedby", description);
    else trigger.removeAttribute("aria-describedby");
    const selected = options.filter((option) => option.selected);
    const label = control.multiple
      ? !selected.length ? "未选择" : selected.length === options.length ? select.dataset.allLabel || "全部" : selected.length === 1 ? selected[0].label : `已选 ${selected.length} 项`
      : selected[0]?.label || "请选择";
    control.valueElement.textContent = label;
    trigger.dataset.tooltip = select.dataset.tooltip?.trim() || label;
    trigger.dataset.tooltipOverflow = ".studio-select-value";
    control.validation.textContent = select.validationMessage || "";
    if (select.validity.valid) { wrapper.classList.remove("is-invalid"); trigger.removeAttribute("aria-invalid"); }
    if (opened === control) {
      if (!isVisible(control) || disabled) close();
      else {
        if (!options.some((option) => option.index === control.activeIndex && !option.disabled && !option.hidden)) control.activeIndex = enabledOptions(control)[0]?.index ?? -1;
        renderMenu(control);
        schedulePosition();
      }
    }
  }

  function enhance(select) {
    if (controls.has(select)) return controls.get(select);
    if (select.tagName === "INPUT") return enhanceEditable(select);
    const wrapper = document.createElement("span");
    wrapper.className = "studio-select";
    const trigger = document.createElement("button");
    trigger.type = "button"; trigger.className = "studio-select-trigger";
    trigger.setAttribute("role", "combobox"); trigger.setAttribute("aria-haspopup", "listbox"); trigger.setAttribute("aria-expanded", "false");
    const identifier = `studio-select-${++sequence}`;
    trigger.id = `${identifier}-trigger`;
    trigger.setAttribute("aria-controls", `${identifier}-menu`);
    const valueElement = document.createElement("span"); valueElement.className = "studio-select-value";
    const arrow = document.createElement("span"); arrow.className = "studio-select-chevron"; arrow.setAttribute("aria-hidden", "true");
    const validation = document.createElement("span"); validation.id = `${identifier}-validation`; validation.className = "studio-select-validation"; validation.setAttribute("role", "alert");
    trigger.append(valueElement, arrow);
    select.before(wrapper); wrapper.append(select, trigger, validation);
    select.classList.add("studio-select-native"); select.setAttribute("aria-hidden", "true"); select.tabIndex = -1;
    const control = { select, wrapper, trigger, valueElement, validation, identifier, options: [], activeIndex: -1, fingerprint: "", menu: null };
    controls.set(select, control);
    const focus = (options) => { update(control); if (!trigger.disabled) trigger.focus(options); };
    select.focus = focus;
    const nativeReportValidity = select.reportValidity.bind(select);
    select.reportValidity = () => {
      if (!select.validity.valid) {
        control.wrapper.classList.add("is-invalid"); trigger.setAttribute("aria-invalid", "true");
        validation.textContent = select.validationMessage || "请选择有效选项。";
        trigger.setAttribute("aria-describedby", `${select.getAttribute("aria-describedby") || ""} ${validation.id}`.trim());
        focus();
      }
      return nativeReportValidity();
    };
    select.addEventListener("invalid", (event) => {
      event.preventDefault();
      wrapper.classList.add("is-invalid"); trigger.setAttribute("aria-invalid", "true");
      validation.textContent = select.validationMessage || "请选择有效选项。";
      trigger.setAttribute("aria-describedby", `${select.getAttribute("aria-describedby") || ""} ${validation.id}`.trim());
      focus();
    });
    select.addEventListener("focus", () => focus());
    select.addEventListener("click", (event) => { event.preventDefault(); focus(); if (opened !== control) open(control); });
    select.addEventListener("input", () => update(control));
    select.addEventListener("change", () => update(control));
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      if (opened === control) close(); else open(control);
    });
    trigger.addEventListener("keydown", (event) => {
      if (opened === control) return;
      if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault(); event.stopPropagation(); open(control, event.key === "ArrowUp" ? -1 : 1);
      } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault(); open(control); search(control, event.key);
      }
    });
    select.form?.addEventListener("reset", () => window.setTimeout(() => update(control), 0));
    update(control);
    return control;
  }

  function updateEditable(control) {
    const { select: input, trigger, wrapper } = control;
    if (input.getAttribute("list")) {
      control.listId = input.getAttribute("list"); input.dataset.studioDatalist = control.listId; input.removeAttribute("list");
    }
    const options = optionData(input);
    const disabled = input.matches(":disabled");
    const hidden = input.hidden || input.classList.contains("is-hidden") || input.style.display === "none";
    const name = controlName(input);
    const fingerprint = JSON.stringify([input.value, options, disabled, hidden, name, input.required]);
    if (fingerprint === control.fingerprint) {
      if (opened === control && (!isVisible(control) || disabled)) close();
      return;
    }
    control.fingerprint = fingerprint; control.options = options; wrapper.hidden = hidden; control.toggle.disabled = disabled;
    trigger.setAttribute("aria-disabled", String(disabled)); trigger.setAttribute("aria-required", String(input.required));
    if (!input.hasAttribute("aria-label") && !input.hasAttribute("aria-labelledby")) trigger.setAttribute("aria-label", name);
    control.toggle.setAttribute("aria-label", `显示${name}选项`);
    if (opened === control) {
      if (!isVisible(control) || disabled) close();
      else { renderMenu(control); schedulePosition(); }
    }
  }

  function enhanceEditable(input) {
    const listId = input.getAttribute("list") || input.dataset.studioDatalist;
    const identifier = `studio-select-${++sequence}`;
    const wrapper = document.createElement("span"); wrapper.className = "studio-select studio-editable-combobox"; wrapper.dataset.selectId = input.id || "";
    const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "studio-combobox-toggle"; toggle.tabIndex = -1;
    const arrow = document.createElement("span"); arrow.className = "studio-select-chevron"; arrow.setAttribute("aria-hidden", "true"); toggle.appendChild(arrow);
    input.before(wrapper); wrapper.append(input, toggle);
    input.dataset.studioDatalist = listId; input.removeAttribute("list"); input.classList.add("studio-combobox-input");
    input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list"); input.setAttribute("aria-haspopup", "listbox"); input.setAttribute("aria-expanded", "false"); input.setAttribute("aria-controls", `${identifier}-menu`);
    const control = { select: input, trigger: input, wrapper, toggle, identifier, listId, editable: true, showAll: false, options: [], activeIndex: -1, fingerprint: "", menu: null };
    controls.set(input, control);
    input.addEventListener("input", () => {
      control.showAll = false; control.activeIndex = -1; update(control);
      if (control.committing) return;
      if (input.isConnected && !input.matches(":disabled") && opened !== control) open(control);
    });
    input.addEventListener("click", () => { if (opened !== control) { control.showAll = !input.value; open(control); } });
    input.addEventListener("change", () => update(control));
    input.addEventListener("keydown", (event) => {
      if (opened === control || event.isComposing || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation(); control.showAll = true; open(control, event.key === "ArrowUp" ? -1 : 1);
      move(control, event.key === "ArrowUp" ? 1 : -1, true);
    });
    toggle.addEventListener("pointerdown", (event) => event.preventDefault());
    toggle.addEventListener("click", () => { if (opened === control) close({ focus: true }); else { control.showAll = true; open(control); } });
    input.form?.addEventListener("reset", () => window.setTimeout(() => update(control), 0));
    update(control);
    return control;
  }

  function enabledOptions(control) { return control.options.filter((option) => !option.disabled && !option.hidden); }

  function markActive(control, index, scroll = false) {
    const option = control.options.find((entry) => entry.index === index);
    if (!option || option.disabled || option.hidden) {
      control.activeIndex = -1; control.trigger.removeAttribute("aria-activedescendant"); control.menu?.querySelectorAll(".is-active").forEach((entry) => entry.classList.remove("is-active"));
      return;
    }
    control.activeIndex = index;
    control.trigger.setAttribute("aria-activedescendant", `${control.identifier}-option-${index}`);
    control.menu?.querySelectorAll("[role=option]").forEach((entry) => entry.classList.toggle("is-active", Number(entry.dataset.optionIndex) === index));
    if (scroll) {
      const row = control.menu?.querySelector(`[data-option-index="${index}"]`);
      const viewport = control.listbox;
      if (row && viewport) {
        const bounds = viewport.getBoundingClientRect(); const target = row.getBoundingClientRect();
        if (target.top < bounds.top) viewport.scrollTop -= bounds.top - target.top;
        else if (target.bottom > bounds.bottom) viewport.scrollTop += target.bottom - bounds.bottom;
      }
    }
  }

  function renderMenu(control) {
    const menu = control.menu; if (!menu) return;
    const scrollTop = control.listbox?.scrollTop || 0;
    menu.replaceChildren();
    menu.classList.toggle("is-multiple", !!control.multiple);
    menu.classList.toggle("is-gallery-filter", GALLERY_SELECT_IDS.has(control.select?.id));
    menu.id = `${control.identifier}-${control.multiple ? "popup" : "menu"}`;
    menu.setAttribute("role", control.multiple ? "group" : "listbox");
    const listbox = document.createElement("div"); listbox.className = "studio-select-options";
    control.listbox = listbox;
    if (control.multiple) {
      const actions = document.createElement("div"); actions.className = "studio-select-actions";
      const enabled = enabledOptions(control);
      for (const [action, label, shortcut] of [["all", "全选", "Ctrl+A"], ["clear", "清空", "Ctrl+Shift+A"]]) {
        const button = document.createElement("button"); button.type = "button"; button.tabIndex = -1; button.dataset.selectAction = action; button.textContent = label; button.dataset.tooltip = `${label}（${shortcut}）`;
        button.disabled = !enabled.some((option) => option.selected !== (action === "all"));
        actions.appendChild(button);
      }
      if (GALLERY_SELECT_IDS.has(control.select.id)) {
        const button = document.createElement("button"); button.type = "button"; button.tabIndex = -1; button.dataset.selectAction = "default"; button.textContent = "设为默认";
        button.disabled = !!control.savingDefault || !enabled.some((option) => option.selected);
        button.dataset.tooltip = control.savingDefault ? "正在保存筛选默认值" : button.disabled ? "至少选择一项后才能设为默认" : "保存当前筛选，下次打开页面时使用（Ctrl+Enter）";
        actions.appendChild(button);
      }
      listbox.id = `${control.identifier}-menu`; listbox.setAttribute("role", "listbox"); listbox.setAttribute("aria-label", controlName(control.select)); listbox.setAttribute("aria-multiselectable", "true");
      menu.appendChild(actions);
    } else listbox.setAttribute("role", "presentation");
    menu.appendChild(listbox);
    let group = "";
    const visible = control.options.filter((option) => !option.hidden);
    if (!visible.length) {
      const empty = document.createElement("div"); empty.className = "studio-select-empty"; empty.textContent = control.editable ? "没有匹配选项，保留当前输入" : "暂无可选项"; listbox.appendChild(empty);
    }
    for (const option of visible) {
      if (option.group && option.group !== group) {
        const heading = document.createElement("div"); heading.className = "studio-select-group"; heading.textContent = option.group; heading.setAttribute("role", "presentation"); listbox.appendChild(heading);
      }
      group = option.group;
      const row = document.createElement("div"); row.className = "studio-select-option"; row.id = `${control.identifier}-option-${option.index}`;
      row.dataset.optionIndex = String(option.index); row.setAttribute("role", "option"); row.setAttribute("aria-selected", String(control.multiple ? option.selected : option.index === selectedIndex(control))); row.setAttribute("aria-disabled", String(option.disabled));
      const mark = document.createElement("span"); mark.className = "studio-select-mark"; mark.setAttribute("aria-hidden", "true"); mark.innerHTML = checkIcon();
      const label = document.createElement("span"); label.className = "studio-select-option-label"; label.textContent = option.label;
      if (control.editable && option.value !== option.label) { const detail = document.createElement("small"); detail.className = "studio-select-option-detail"; detail.textContent = option.value; label.appendChild(detail); }
      row.append(mark, label); listbox.appendChild(row);
    }
    listbox.scrollTop = scrollTop;
    markActive(control, control.activeIndex);
  }

  function open(control, direction = 1) {
    update(control);
    if (control.trigger.disabled || !isVisible(control)) return;
    if (opened) close();
    opened = control;
    const enabled = enabledOptions(control);
    control.activeIndex = control.editable ? -1 : enabled.find((option) => option.index === selectedIndex(control))?.index ?? (direction < 0 ? enabled.at(-1)?.index : enabled[0]?.index) ?? -1;
    const menu = document.createElement("div"); menu.id = `${control.identifier}-menu`; menu.className = "studio-select-menu"; menu.setAttribute("role", "listbox"); menu.setAttribute("aria-label", controlName(control.select));
    menu.dataset.selectId = control.select.id || "";
    control.menu = menu; document.body.appendChild(menu);
    control.trigger.setAttribute("aria-expanded", "true"); control.wrapper.classList.add("is-open");
    control.trigger.focus({ preventScroll: true });
    renderMenu(control); positionMenu(); markActive(control, control.activeIndex, true);
    menu.addEventListener("pointerdown", (event) => { if (event.pointerType === "mouse") event.preventDefault(); });
    menu.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "mouse") return;
      const option = event.target.closest("[data-option-index]"); if (option) markActive(control, Number(option.dataset.optionIndex));
    });
    menu.addEventListener("click", (event) => {
      const action = event.target.closest("[data-select-action]");
      if (action && !action.disabled) {
        if (action.dataset.selectAction === "default") saveGalleryDefault(control);
        else chooseAll(control, action.dataset.selectAction === "all");
        return;
      }
      const option = event.target.closest("[data-option-index]"); if (option) choose(control, Number(option.dataset.optionIndex));
    });
    menu.addEventListener("touchstart", (event) => { if (event.touches.length === 1) lastTouchY = event.touches[0].clientY; }, { passive: true });
    menu.addEventListener("touchmove", (event) => {
      if (event.touches.length !== 1) return;
      const y = event.touches[0].clientY; const movement = lastTouchY - y; lastTouchY = y;
      if (!control.listbox?.contains(event.target) || atScrollBoundary(control.listbox, movement)) event.preventDefault();
      event.stopPropagation();
    }, { passive: false });
    menu.addEventListener("wheel", (event) => {
      if (!control.listbox?.contains(event.target) || atScrollBoundary(control.listbox, event.deltaY)) event.preventDefault();
      event.stopPropagation();
    }, { passive: false });
  }

  function atScrollBoundary(menu, delta) {
    return menu.scrollHeight <= menu.clientHeight + 1 || delta < 0 && menu.scrollTop <= 0 || delta > 0 && menu.scrollTop + menu.clientHeight >= menu.scrollHeight - 1;
  }

  function close(options = {}) {
    if (!opened) return;
    const control = opened; opened = null;
    control.menu?.remove(); control.menu = null; control.listbox = null; control.wrapper.classList.remove("is-open");
    control.trigger.setAttribute("aria-expanded", "false"); control.trigger.removeAttribute("aria-activedescendant");
    typeahead = ""; typeaheadAt = 0;
    if (options.focus && control.trigger.isConnected && !control.trigger.disabled) control.trigger.focus({ preventScroll: true });
  }

  function choose(control, index) {
    const option = control.options.find((entry) => entry.index === index);
    if (!option || option.hidden || option.disabled) return;
    if (control.multiple) {
      control.select.options[index].selected = !option.selected;
      control.activeIndex = index;
      update(control); control.trigger.focus({ preventScroll: true });
      commitSelection(control);
      return;
    }
    const changed = control.editable ? control.select.value !== option.value : control.select.selectedIndex !== index;
    if (control.editable) control.select.value = option.value;
    else control.select.selectedIndex = index;
    close({ focus: true }); update(control);
    if (changed) {
      control.committing = true;
      try {
        control.select.dispatchEvent(new Event("input", { bubbles: true }));
        if (control.select.isConnected && control.wrapper.contains(control.select) && control.select.value === option.value) control.select.dispatchEvent(new Event("change", { bubbles: true }));
      } finally { control.committing = false; }
    }
    refresh();
  }

  function chooseAll(control, selected) {
    if (!control.multiple) return;
    let changed = false;
    for (const option of enabledOptions(control)) {
      if (option.selected === selected) continue;
      control.select.options[option.index].selected = selected;
      changed = true;
    }
    update(control); control.trigger.focus({ preventScroll: true });
    if (changed) commitSelection(control);
  }

  function commitSelection(control) {
    const selection = () => JSON.stringify(Array.from(control.select.options, (option) => [option.value, option.selected]));
    const expected = selection();
    control.committing = true;
    try {
      control.select.dispatchEvent(new Event("input", { bubbles: true }));
      if (control.select.isConnected && control.wrapper.contains(control.select) && selection() === expected) control.select.dispatchEvent(new Event("change", { bubbles: true }));
    } finally { control.committing = false; }
    refresh();
  }

  function move(control, direction, edge = false) {
    const options = enabledOptions(control); if (!options.length) return;
    const current = options.findIndex((option) => option.index === control.activeIndex);
    const index = edge ? direction < 0 ? 0 : options.length - 1 : current < 0 ? direction < 0 ? options.length - 1 : 0 : Math.max(0, Math.min(options.length - 1, current + direction));
    markActive(control, options[index].index, true);
  }

  function search(control, character) {
    const now = Date.now(); typeahead = now - typeaheadAt > 650 ? character : typeahead + character; typeaheadAt = now;
    const options = enabledOptions(control);
    const repeated = typeahead.split("").every((part) => part === typeahead[0]);
    const query = (repeated ? character : typeahead).toLocaleLowerCase();
    const current = options.findIndex((option) => option.index === control.activeIndex);
    const order = repeated ? [...options.slice(current + 1), ...options.slice(0, current + 1)] : options;
    const found = order.find((option) => option.label.trim().toLocaleLowerCase().startsWith(query));
    if (found) markActive(control, found.index, true);
  }

  function positionMenu() {
    frame = 0;
    const control = opened; if (!control) return;
    if (!isVisible(control) || control.select.matches(":disabled")) { close(); return; }
    const menu = control.menu; const rect = control.trigger.getBoundingClientRect(); const viewport = window.visualViewport;
    const leftEdge = (viewport?.offsetLeft || 0) + 8; const topEdge = (viewport?.offsetTop || 0) + 8;
    const rightEdge = (viewport?.offsetLeft || 0) + (viewport?.width || document.documentElement.clientWidth) - 8;
    const bottomEdge = (viewport?.offsetTop || 0) + (viewport?.height || window.innerHeight) - 8;
    if (rect.bottom < topEdge || rect.top > bottomEdge || rect.right < leftEdge || rect.left > rightEdge) { close(); return; }
    const width = Math.max(0, Math.min(Math.max(rect.width, 180), rightEdge - leftEdge));
    menu.style.width = `${width}px`;
    const contentHeight = (control.listbox?.scrollHeight || 0) + (menu.querySelector(".studio-select-actions")?.offsetHeight || 0) + 2;
    const naturalHeight = Math.min(contentHeight || 44, 300);
    const below = Math.max(0, bottomEdge - rect.bottom - 5);
    const above = Math.max(0, rect.top - topEdge - 5);
    const placeAbove = below < naturalHeight && above > below;
    const maxHeight = Math.max(36, Math.min(300, placeAbove ? above : below, bottomEdge - topEdge));
    menu.style.maxHeight = `${maxHeight}px`;
    const height = Math.min(contentHeight, maxHeight);
    const top = placeAbove ? rect.top - height - 5 : rect.bottom + 5;
    menu.style.left = `${Math.max(leftEdge, Math.min(rect.left, rightEdge - width))}px`;
    menu.style.top = `${Math.max(topEdge, Math.min(top, bottomEdge - height))}px`;
    menu.dataset.placement = placeAbove ? "above" : "below";
  }

  function schedulePosition() { if (!frame && opened) frame = requestAnimationFrame(positionMenu); }

  function refresh(root = document) {
    for (const [select, control] of controls) {
      if (!select.isConnected || !control.wrapper.contains(select)) {
        if (opened === control) close();
        controls.delete(select);
      }
    }
    const selects = root.matches?.(SOURCE_SELECTOR) ? [root] : Array.from(root.querySelectorAll?.(SOURCE_SELECTOR) || []);
    for (const select of selects) update(enhance(select));
    if (opened) { update(opened); schedulePosition(); }
  }

  function queueRefresh() {
    if (pendingRefresh) return;
    pendingRefresh = true;
    queueMicrotask(() => { pendingRefresh = false; refresh(); });
  }

  function relevantMutation(record) {
    const target = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
    if (target?.matches("select, option, optgroup, fieldset, datalist, input[list], input[data-studio-datalist]") || target?.closest("select, datalist")) return true;
    if (record.type === "childList") return Array.from(record.addedNodes).some((node) => node.nodeType === Node.ELEMENT_NODE && (node.matches(`${SOURCE_SELECTOR}, datalist`) || !node.matches(".studio-select, .studio-select-menu") && !!node.querySelector(SOURCE_SELECTOR)));
    return false;
  }

  function start() {
    refresh();
    const observer = new MutationObserver((records) => {
      if (records.some(relevantMutation)) queueRefresh();
      else if (opened && records.some((record) => record.type === "childList" && record.removedNodes.length || record.type === "attributes" && record.target.contains?.(opened.trigger))) schedulePosition();
    });
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRIBUTE_NAMES });
    document.addEventListener("pointerdown", (event) => {
      if (opened && !opened.wrapper.contains(event.target) && !opened.menu?.contains(event.target)) close();
    }, true);
    document.addEventListener("focusin", (event) => {
      if (opened && !opened.wrapper.contains(event.target) && !opened.menu?.contains(event.target)) close();
    }, true);
    document.addEventListener("scroll", (event) => { if (opened && !opened.menu?.contains(event.target)) schedulePosition(); }, true);
    window.addEventListener("resize", schedulePosition);
    window.visualViewport?.addEventListener("resize", schedulePosition);
    window.visualViewport?.addEventListener("scroll", schedulePosition);
  }

  // Handle menu keys before the application's dialog Escape handler.
  window.addEventListener("keydown", (event) => {
    const control = opened; if (!control) return;
    if (event.isComposing) return;
    if (event.key === "Tab") { close(); return; }
    if (control.multiple && GALLERY_SELECT_IDS.has(control.select.id) && event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.altKey) {
      event.preventDefault(); event.stopImmediatePropagation(); saveGalleryDefault(control); return;
    }
    if (control.multiple && event.key.toLocaleLowerCase() === "a" && (event.ctrlKey || event.metaKey) && !event.altKey) {
      event.preventDefault(); event.stopImmediatePropagation(); chooseAll(control, !event.shiftKey); return;
    }
    if (control.editable && !["Escape", "ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
    if (control.editable && event.key === "Enter" && control.activeIndex < 0) { close(); return; }
    if (!["Escape", "ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key) && (event.key.length !== 1 || event.ctrlKey || event.altKey || event.metaKey)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.key === "Escape") close({ focus: true });
    else if (event.key === "ArrowDown") move(control, 1);
    else if (event.key === "ArrowUp") move(control, -1);
    else if (event.key === "Home") move(control, -1, true);
    else if (event.key === "End") move(control, 1, true);
    else if (event.key === "Enter" || event.key === " ") choose(control, control.activeIndex);
    else search(control, event.key);
  }, true);

  window.ImageStudioSelect = { refresh, close, isOpen: () => !!opened, getGalleryDefault };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
