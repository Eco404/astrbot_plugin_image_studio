(function () {
  "use strict";

  const bindings = new WeakMap();

  window.ImageStudioSortable = {
    bind(grid, options = {}) {
      bindings.get(grid)?.destroy();
      const itemSelector = options.itemSelector || "[data-import-id]";
      const handleSelector = options.handleSelector || "[data-sort-handle]";
      const getId = options.getId || ((item) => item.dataset.importId);
      const items = () => Array.from(grid.children).filter((item) => item.matches(itemSelector));
      const enabled = () => grid.isConnected && options.isEnabled?.() !== false && items().length > 1;
      let drag = null;
      let animation = 0;
      let suppressClick = false;
      const live = document.createElement("span");
      live.className = "studio-sort-status";
      live.setAttribute("role", "status");
      live.setAttribute("aria-live", "polite");
      grid.after(live);

      function clearTarget() {
        drag?.target?.classList.remove("is-sort-target-before", "is-sort-target-after");
        if (drag) drag.target = null;
      }

      function scrollParent(element) {
        for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
          if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) return parent;
        }
        return document.scrollingElement;
      }

      function targetAt(x, y) {
        const visible = document.elementsFromPoint(x, y);
        const target = visible.map((element) => element.closest(itemSelector)).find((item) => item?.parentElement === grid);
        if (!target || target === drag.item) { clearTarget(); return; }
        if (target === drag.target) return;
        clearTarget();
        const order = items();
        drag.target = target;
        drag.after = order.indexOf(target) > order.indexOf(drag.item);
        target.classList.add(drag.after ? "is-sort-target-after" : "is-sort-target-before");
        live.textContent = `松开移到第 ${order.indexOf(target) + 1} 张`;
      }

      function tick() {
        animation = 0;
        if (!drag?.started) return;
        if (!enabled() || drag.item.parentElement !== grid || !drag.capture.isConnected) { cancel(); return; }
        const viewport = window.visualViewport;
        const viewLeft = viewport?.offsetLeft || 0;
        const viewTop = viewport?.offsetTop || 0;
        const viewWidth = viewport?.width || window.innerWidth;
        const viewHeight = viewport?.height || window.innerHeight;
        const box = drag.ghost.getBoundingClientRect();
        const left = Math.max(viewLeft + 6, Math.min(drag.x - drag.offsetX, viewLeft + viewWidth - box.width - 6));
        const top = Math.max(viewTop + 6, Math.min(drag.y - drag.offsetY, viewTop + viewHeight - box.height - 6));
        drag.ghost.style.transform = `translate3d(${left}px, ${top}px, 0)`;
        const scroller = drag.scroller;
        const bounds = scroller === document.scrollingElement ? { top: viewTop, bottom: viewTop + viewHeight } : scroller.getBoundingClientRect();
        const minY = Math.max(viewTop, bounds.top), maxY = Math.min(viewTop + viewHeight, bounds.bottom);
        const edge = Math.min(64, (maxY - minY) / 4);
        const direction = drag.y < minY + edge ? -1 : drag.y > maxY - edge ? 1 : 0;
        if (direction && drag.x >= viewLeft && drag.x <= viewLeft + viewWidth) {
          scroller.scrollTop = Math.max(0, Math.min(scroller.scrollHeight - scroller.clientHeight, scroller.scrollTop + direction * 12));
        }
        targetAt(drag.x, drag.y);
        animation = requestAnimationFrame(tick);
      }

      function start() {
        drag.started = true;
        const box = drag.item.getBoundingClientRect();
        const ghost = document.createElement("div");
        ghost.className = "studio-sort-ghost glass";
        ghost.setAttribute("aria-hidden", "true");
        ghost.style.width = `${Math.min(220, box.width, window.innerWidth - 20)}px`;
        const original = drag.item.querySelector(".import-card-preview img, img");
        if (original) {
          const image = document.createElement("img");
          image.src = original.currentSrc || original.src;
          image.alt = ""; ghost.appendChild(image);
        }
        const label = document.createElement("span");
        label.textContent = drag.item.querySelector(".import-card-header strong")?.textContent || "移动图片";
        ghost.appendChild(label); document.body.appendChild(ghost);
        drag.ghost = ghost;
        drag.offsetX = ghost.getBoundingClientRect().width / 2;
        drag.offsetY = 24;
        drag.item.classList.add("is-sort-dragging");
        drag.capture.setAttribute("aria-pressed", "true");
        grid.classList.add("is-sorting");
        live.textContent = "正在移动图片，松开以确认，按 Escape 取消";
        tick();
      }

      function finish(commit) {
        if (!drag) return;
        const current = drag;
        const target = current.target;
        const valid = commit && enabled() && current.item.parentElement === grid && target?.parentElement === grid;
        clearTarget(); drag = null;
        cancelAnimationFrame(animation); animation = 0;
        current.ghost?.remove();
        current.item.classList.remove("is-sort-dragging");
        current.capture.removeAttribute("aria-pressed");
        grid.classList.remove("is-sorting");
        if (current.capture.hasPointerCapture?.(current.pointerId)) current.capture.releasePointerCapture(current.pointerId);
        if (current.started) {
          suppressClick = true;
          setTimeout(() => { suppressClick = false; }, 0);
        }
        if (valid) {
          grid.insertBefore(current.item, current.after ? target.nextSibling : target);
          announceAndCommit(current.item);
        } else if (current.started) live.textContent = "已取消图片移动";
        options.onDragEnd?.();
      }

      function announceAndCommit(item) {
        const order = items();
        live.textContent = `图片已移到第 ${order.indexOf(item) + 1} 张，共 ${order.length} 张`;
        options.onReorder?.(order.map(getId));
      }

      function cancel() { finish(false); }

      function pointerDown(event) {
        if (event.button !== 0 || event.isPrimary === false || !enabled() || drag) return;
        const handle = event.target.closest(handleSelector);
        const surface = event.pointerType === "mouse" ? event.target.closest("[data-sort-surface]") : null;
        const capture = handle || surface;
        const item = capture?.closest(itemSelector);
        if (!capture || item?.parentElement !== grid || capture.matches(":disabled") || event.target.closest("input, textarea, select, a")) return;
        event.preventDefault();
        handle?.focus({ preventScroll: true });
        drag = { pointerId: event.pointerId, capture, item, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, scroller: scrollParent(grid), started: false, target: null };
        capture.setPointerCapture?.(event.pointerId);
      }

      function pointerMove(event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        drag.x = event.clientX; drag.y = event.clientY;
        if (!drag.started && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) >= 6) start();
        if (drag.started) event.preventDefault();
      }

      function pointerUp(event) { if (drag?.pointerId === event.pointerId) { drag.x = event.clientX; drag.y = event.clientY; if (drag.started) targetAt(drag.x, drag.y); finish(!!drag?.started); } }
      function pointerCancel(event) { if (drag?.pointerId === event.pointerId) cancel(); }
      function keyDown(event) {
        if (event.key === "Escape" && drag) { event.preventDefault(); event.stopImmediatePropagation(); cancel(); return; }
        const handle = event.target.closest(handleSelector);
        const item = handle?.closest(itemSelector);
        if (!enabled() || item?.parentElement !== grid || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation();
        const order = items(), from = order.indexOf(item);
        const to = event.key === "Home" ? 0 : event.key === "End" ? order.length - 1 : Math.max(0, Math.min(order.length - 1, from + (["ArrowUp", "ArrowLeft"].includes(event.key) ? -1 : 1)));
        if (from === to) return;
        grid.insertBefore(item, to > from ? order[to].nextSibling : order[to]);
        announceAndCommit(item);
        items().find(node => getId(node) === getId(item))?.querySelector(handleSelector)?.focus({ preventScroll: true });
      }
      function click(event) { if (suppressClick) { event.preventDefault(); event.stopImmediatePropagation(); } }
      function nativeDrag(event) { if (event.target.closest("[data-sort-surface]")) event.preventDefault(); }
      grid.addEventListener("pointerdown", pointerDown);
      grid.addEventListener("dragstart", nativeDrag);
      grid.addEventListener("lostpointercapture", pointerCancel);
      grid.addEventListener("click", click, true);
      window.addEventListener("pointermove", pointerMove, { passive: false });
      window.addEventListener("pointerup", pointerUp);
      window.addEventListener("pointercancel", pointerCancel);
      window.addEventListener("blur", cancel);
      window.addEventListener("keydown", keyDown, true);
      const binding = { cancel, isDragging: () => !!drag, destroy() {
        cancel(); live.remove();
        grid.removeEventListener("pointerdown", pointerDown);
        grid.removeEventListener("dragstart", nativeDrag);
        grid.removeEventListener("lostpointercapture", pointerCancel);
        grid.removeEventListener("click", click, true);
        window.removeEventListener("pointermove", pointerMove);
        window.removeEventListener("pointerup", pointerUp);
        window.removeEventListener("pointercancel", pointerCancel);
        window.removeEventListener("blur", cancel);
        window.removeEventListener("keydown", keyDown, true);
        bindings.delete(grid);
      } };
      bindings.set(grid, binding);
      return binding;
    },
  };
})();
