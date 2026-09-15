(function () {
  "use strict";

  const ID = "studioTooltip";
  const SELECTOR = "[data-tooltip]";
  const SHOW_DELAY = 600;
  const HELP_SHOW_DELAY = 240;
  const LEAVE_DELAY = 160;
  let popup = null, active = null, pending = null;
  let showTimer = 0, leaveTimer = 0, exitTimer = 0;
  let pinned = false, pointerTrigger = null, focusTrigger = null;
  let pointerDownTarget = null, pointerDownAt = 0, touchY = 0;
  let keyboardFocusPending = false, keyboardFocusTimer = 0;

  const elementOf = (node) => node instanceof Element ? node : node?.parentElement;
  const triggerOf = (node) => elementOf(node)?.closest(SELECTOR) || null;
  const insidePopup = (node) => !!popup && node instanceof Node && popup.contains(node);
  const contentOf = (trigger) => trigger?.getAttribute("data-tooltip") || "";
  const normalizedText = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const clipped = (element) => element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1;

  function useful(trigger) {
    const content = normalizedText(contentOf(trigger));
    if (!content) return false;
    if (trigger.hasAttribute("data-tooltip-toggle")) return true;
    if (trigger.hasAttribute("data-tooltip-overflow")) {
      const selector = trigger.getAttribute("data-tooltip-overflow");
      const label = selector ? trigger.querySelector(selector) : trigger;
      if (!label) return false;
      // An explicit description or disabled reason still adds useful context.
      if (content !== normalizedText(label.textContent)) return true;
      return !label.getClientRects().length || getComputedStyle(label).visibility === "hidden" || clipped(label);
    }
    // Do not repeat a fully readable label. Icon-only actions and additional
    // explanations still retain their hints and accessible names.
    return content !== normalizedText(trigger.innerText) || clipped(trigger);
  }

  function visible(trigger) {
    return !!trigger?.isConnected && !!trigger.getClientRects().length
      && !trigger.closest('[hidden], [inert], [aria-hidden="true"], .is-hidden, .is-closing')
      && !(trigger.getAttribute("aria-expanded") === "true" && !trigger.hasAttribute("data-tooltip-toggle"))
      && getComputedStyle(trigger).visibility !== "hidden";
  }

  function describe(trigger, add) {
    if (!trigger) return;
    const ids = (trigger.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    const next = ids.filter((id) => id !== ID);
    if (add) next.push(ID);
    if (next.length) trigger.setAttribute("aria-describedby", next.join(" "));
    else trigger.removeAttribute("aria-describedby");
  }

  function hide(immediate = false) {
    clearTimeout(showTimer); clearTimeout(leaveTimer); clearTimeout(exitTimer);
    showTimer = leaveTimer = exitTimer = 0;
    pending = null;
    describe(active, false);
    active = null; pinned = false;
    if (!popup) return;
    popup.classList.remove("is-visible");
    popup.setAttribute("aria-hidden", "true");
    if (immediate || matchMedia("(prefers-reduced-motion: reduce)").matches) popup.hidden = true;
    else exitTimer = setTimeout(() => { popup.hidden = true; }, 120);
  }

  function containScroll(event, delta) {
    // Include a JS boundary guard for iOS versions without overscroll-behavior.
    const atStart = popup.scrollTop <= 0;
    const atEnd = popup.scrollTop + popup.clientHeight >= popup.scrollHeight - 1;
    if ((delta < 0 && atStart || delta > 0 && atEnd) && event.cancelable) event.preventDefault();
    event.stopPropagation();
  }

  function ensurePopup() {
    if (popup) return;
    popup = document.createElement("div");
    popup.id = ID; popup.className = "studio-tooltip";
    popup.setAttribute("role", "tooltip"); popup.setAttribute("aria-hidden", "true");
    popup.hidden = true;
    popup.addEventListener("pointerenter", () => clearTimeout(leaveTimer));
    popup.addEventListener("pointerleave", scheduleLeave);
    popup.addEventListener("wheel", (event) => containScroll(event, event.deltaY), { passive: false });
    popup.addEventListener("touchstart", (event) => {
      if (event.touches.length === 1) touchY = event.touches[0].clientY;
    }, { passive: true });
    popup.addEventListener("touchmove", (event) => {
      if (event.touches.length !== 1) return;
      const delta = touchY - event.touches[0].clientY;
      touchY = event.touches[0].clientY;
      containScroll(event, delta);
    }, { passive: false });
    document.body.append(popup);
  }

  function position() {
    if (!active || !visible(active)) { hide(); return; }
    const viewport = window.visualViewport;
    const originX = viewport?.offsetLeft || 0, originY = viewport?.offsetTop || 0;
    const width = viewport?.width || document.documentElement.clientWidth;
    const height = viewport?.height || document.documentElement.clientHeight;
    const margin = 10, gap = 7;
    const anchor = active.getBoundingClientRect();
    const leftBound = originX + margin, topBound = originY + margin;
    const rightBound = originX + width - margin, bottomBound = originY + height - margin;
    popup.style.maxWidth = `${Math.max(0, Math.min(380, width - margin * 2))}px`;
    popup.style.maxHeight = `${Math.max(0, Math.min(360, height - margin * 2))}px`;
    const measured = popup.getBoundingClientRect();
    const above = Math.max(0, anchor.top - topBound - gap);
    const below = Math.max(0, bottomBound - anchor.bottom - gap);
    const onTop = above >= measured.height || above > below;
    const available = onTop ? above : below;
    popup.style.maxHeight = `${Math.max(24, Math.min(360, available, height - margin * 2))}px`;
    const sized = popup.getBoundingClientRect();
    const left = Math.max(leftBound, Math.min(anchor.left + anchor.width / 2 - sized.width / 2, rightBound - sized.width));
    const preferredTop = onTop ? anchor.top - gap - sized.height : anchor.bottom + gap;
    const top = Math.max(topBound, Math.min(preferredTop, bottomBound - sized.height));
    popup.style.left = `${Math.round(left)}px`;
    popup.style.top = `${Math.round(top)}px`;
  }

  function show(trigger, pin = false) {
    if (!visible(trigger) || !useful(trigger)) return;
    clearTimeout(showTimer); clearTimeout(leaveTimer); clearTimeout(exitTimer);
    showTimer = leaveTimer = exitTimer = 0; pending = null;
    ensurePopup();
    if (active !== trigger) { describe(active, false); popup.scrollTop = 0; }
    active = trigger; pinned = pin;
    // Keep descriptions as plain text. Schema descriptions may contain markup,
    // long request keys or line breaks; none should become executable HTML.
    if (popup.textContent !== contentOf(trigger)) popup.textContent = contentOf(trigger);
    popup.hidden = false;
    popup.setAttribute("aria-hidden", "false");
    describe(trigger, true);
    position();
    if (active) popup.classList.add("is-visible");
  }

  function scheduleShow(trigger) {
    clearTimeout(showTimer);
    pending = null;
    if (pinned || !trigger || !useful(trigger)) return;
    clearTimeout(leaveTimer);
    if (active === trigger) return;
    pending = trigger;
    showTimer = setTimeout(() => { if (pending === trigger) show(trigger); }, trigger.hasAttribute("data-tooltip-toggle") ? HELP_SHOW_DELAY : SHOW_DELAY);
  }

  function scheduleLeave() {
    clearTimeout(showTimer); pending = null;
    if (pinned || active && (pointerTrigger === active || focusTrigger === active)) return;
    clearTimeout(leaveTimer);
    // A small bridge lets the pointer cross the gap into a long tooltip.
    leaveTimer = setTimeout(() => { if (!popup?.matches(":hover")) hide(); }, LEAVE_DELAY);
  }

  function migrateTitle(element) {
    if (!(element instanceof Element) || element.localName === "title" || !element.hasAttribute("title")) return;
    const title = element.getAttribute("title");
    if (element.getAttribute("data-tooltip") !== title) element.setAttribute("data-tooltip", title);
    element.removeAttribute("title");
  }

  function migrateTree(root) {
    if (!(root instanceof Element) || insidePopup(root)) return;
    migrateTitle(root);
    root.querySelectorAll("[title]").forEach(migrateTitle);
  }

  function init() {
    migrateTree(document.body);
    // Only new subtrees and changed attributes are inspected. Third-party
    // controls (including PhotoSwipe) can keep assigning their native titles.
    new MutationObserver((records) => {
      let checkActive = false;
      for (const record of records) {
        if (insidePopup(record.target)) continue;
        if (record.type === "childList") {
          for (const added of record.addedNodes) migrateTree(added);
          if (active && record.removedNodes.length) checkActive = true;
        } else {
          if (record.attributeName === "title") migrateTitle(record.target);
          if (active && (record.target === active || record.target.contains(active))) checkActive = true;
        }
      }
      if (checkActive && active) {
        if (!visible(active) || !useful(active)) hide();
        else if (popup.textContent !== contentOf(active)) show(active, pinned);
      }
      if (pending && !pending.isConnected) { clearTimeout(showTimer); pending = null; }
    }).observe(document.body, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ["title", "data-tooltip", "data-tooltip-overflow", "hidden", "inert", "aria-hidden", "aria-expanded", "class"],
    });

    document.addEventListener("pointerover", (event) => {
      if (event.pointerType === "touch" || insidePopup(event.target)) return;
      const trigger = triggerOf(event.target);
      if (trigger === triggerOf(event.relatedTarget)) return;
      pointerTrigger = trigger;
      scheduleShow(trigger);
    });
    document.addEventListener("pointerout", (event) => {
      if (event.pointerType === "touch" || insidePopup(event.target)) return;
      const from = triggerOf(event.target), to = triggerOf(event.relatedTarget);
      if (from === to) return;
      pointerTrigger = to;
      if (insidePopup(event.relatedTarget)) { clearTimeout(leaveTimer); return; }
      scheduleLeave();
    });
    document.addEventListener("pointerdown", (event) => {
      keyboardFocusPending = false; clearTimeout(keyboardFocusTimer);
      pointerDownTarget = elementOf(event.target); pointerDownAt = performance.now();
      if (insidePopup(event.target)) return;
      const trigger = triggerOf(event.target);
      if (active && (trigger !== active || !trigger?.hasAttribute("data-tooltip-toggle"))) hide();
      clearTimeout(showTimer); pending = null;
    }, true);
    document.addEventListener("focusin", (event) => {
      if (insidePopup(event.target)) return;
      const trigger = triggerOf(event.target);
      // Show focus hints only for an actual Tab navigation. Closing a menu,
      // copying text or rerendering a dialog can restore focus programmatically.
      focusTrigger = keyboardFocusPending ? trigger : null;
      keyboardFocusPending = false; clearTimeout(keyboardFocusTimer);
      if (active && active !== trigger) hide();
      if (focusTrigger) show(focusTrigger);
    });
    document.addEventListener("focusout", (event) => {
      if (insidePopup(event.relatedTarget)) return;
      if (performance.now() - pointerDownAt < 500 && insidePopup(pointerDownTarget)) return;
      if (triggerOf(event.relatedTarget) === focusTrigger) return;
      focusTrigger = null;
      if (pinned && active && !active.contains(event.relatedTarget)) hide();
      else scheduleLeave();
    });
    document.addEventListener("click", (event) => {
      const trigger = triggerOf(event.target);
      if (!trigger?.hasAttribute("data-tooltip-toggle")) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (active === trigger && pinned) hide();
      else show(trigger, true);
    }, true);
    window.addEventListener("keydown", (event) => {
      // A recent click must not suppress a subsequent real keyboard focus,
      // for example when Tab / Shift+Tab immediately returns to that control.
      pointerDownTarget = null; pointerDownAt = 0;
      keyboardFocusPending = event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey;
      clearTimeout(keyboardFocusTimer);
      if (keyboardFocusPending) keyboardFocusTimer = setTimeout(() => { keyboardFocusPending = false; }, 0);
      if (!active) return;
      if (event.key === "Escape") {
        event.preventDefault(); event.stopImmediatePropagation(); hide();
      } else if (active.hasAttribute("data-tooltip-toggle") && triggerOf(event.target) === active
        && popup.scrollHeight > popup.clientHeight && !event.altKey && !event.ctrlKey && !event.metaKey
        && ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) {
        event.preventDefault(); event.stopImmediatePropagation();
        const distance = event.key.startsWith("Page") ? popup.clientHeight * .85 : 40;
        if (event.key === "Home") popup.scrollTop = 0;
        else if (event.key === "End") popup.scrollTop = popup.scrollHeight;
        else popup.scrollTop += ["ArrowUp", "PageUp"].includes(event.key) ? -distance : distance;
      } else if (!active.hasAttribute("data-tooltip-toggle")
        && ["Enter", " ", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.key)) {
        hide();
      }
    }, true);
    document.addEventListener("scroll", (event) => { if (!insidePopup(event.target)) hide(true); }, true);
    window.addEventListener("resize", () => hide(true));
    window.visualViewport?.addEventListener("resize", () => hide(true));
    window.visualViewport?.addEventListener("scroll", () => hide(true));
    window.addEventListener("blur", () => hide(true));
    document.addEventListener("visibilitychange", () => { if (document.hidden) hide(true); });
  }

  window.ImageStudioTooltip = { hide, refresh: () => { if (active) show(active, pinned); } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
