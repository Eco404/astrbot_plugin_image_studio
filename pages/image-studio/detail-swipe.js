(() => {
  "use strict";

  const bindings = new WeakMap();
  const mobile = window.matchMedia("(max-width: 540px)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const velocityWindow = 100;
  const flickVelocity = 0.4;
  const flickDistance = 24;
  let suppressClickUntil = 0;

  function sampleMovement(active, x, time) {
    const previous = active.samples[active.samples.length - 1];
    active.minX = Math.min(active.minX, x);
    active.maxX = Math.max(active.maxX, x);
    active.samples.push({ x, time: Math.max(time, previous.time) });
    const cutoff = time - velocityWindow;
    while (active.samples.length > 2 && active.samples[1].time <= cutoff) active.samples.shift();
  }

  function releaseVelocity(active) {
    const samples = active.samples;
    const end = samples[samples.length - 1];
    const start = samples[0];
    const cutoff = end.time - velocityWindow;
    let x = start.x;
    let time = start.time;
    if (time < cutoff && samples[1].time > time) {
      const next = samples[1];
      const ratio = Math.min(1, (cutoff - time) / (next.time - time));
      x += (next.x - x) * ratio;
      time = cutoff;
    }
    const duration = end.time - time;
    return duration >= 8 ? (end.x - x) / duration : 0;
  }

  function bind(frame, hooks) {
    if (!(frame instanceof HTMLElement)) return () => {};
    bindings.get(frame)?.();

    let gesture = null;
    let overlay = null;
    let track = null;
    let disposed = false;
    let finishAnimation = null;
    let phase = "idle";

    function setPhase(value) {
      phase = value;
      if (value === "idle") delete frame.dataset.detailSwipeState;
      else frame.dataset.detailSwipeState = value;
    }

    function suppressClick() {
      suppressClickUntil = Date.now() + 500;
    }

    function removeOverlay() {
      finishAnimation?.();
      overlay?.remove();
      overlay = null;
      track = null;
      frame.classList.remove("is-detail-swiping", "is-detail-handoff");
    }

    function finish(committed = false, direction = 0) {
      removeOverlay();
      gesture = null;
      setPhase("idle");
      frame.dispatchEvent(new CustomEvent("detail-swipe-end", {
        detail: { committed, direction },
      }));
    }

    function currentSource() {
      const image = frame.querySelector("[data-detail-image], .detail-image");
      return image?.currentSrc || image?.getAttribute("src") || "";
    }

    function neighbor(direction) {
      const value = hooks?.getNeighbor?.(direction);
      return value && typeof value.src === "string" && value.src ? value : null;
    }

    function pane(source, offset) {
      const element = document.createElement("div");
      element.className = "detail-swipe-pane";
      element.dataset.swipeOffset = String(offset);
      element.style.transform = `translate3d(${offset * 100}%, 0, 0)`;
      const image = document.createElement("img");
      image.className = "detail-image detail-swipe-image";
      image.alt = "";
      image.draggable = false;
      image.setAttribute("aria-hidden", "true");
      image.src = source;
      image.addEventListener("error", () => { image.style.visibility = "hidden"; }, { once: true });
      element.append(image);
      return element;
    }

    function createOverlay(active) {
      const source = currentSource();
      active.previous = neighbor(-1);
      active.next = neighbor(1);
      if (!source || !active.width || !frame.clientHeight) return;
      overlay = document.createElement("div");
      overlay.className = "detail-swipe-overlay";
      overlay.setAttribute("aria-hidden", "true");
      track = document.createElement("div");
      track.className = "detail-swipe-track";
      track.style.transform = "translate3d(0px, 0, 0)";
      track.append(pane(source, 0));
      if (active.previous) track.append(pane(active.previous.src, -1));
      if (active.next) track.append(pane(active.next.src, 1));
      overlay.append(track);
      frame.append(overlay);
      frame.classList.add("is-detail-swiping");
      frame.dispatchEvent(new CustomEvent("detail-swipe-start"));
      active.pending = new Map();
      if (hooks?.prepareNeighbor) {
        for (const direction of [-1, 1]) {
          const promise = Promise.resolve().then(() => hooks.prepareNeighbor(direction)).then((value) => {
            if (disposed || gesture !== active || !track || !value?.src) return value;
            const key = direction < 0 ? "previous" : "next";
            if (!active[key]) track.append(pane(value.src, direction));
            active[key] = value;
            if (phase === "dragging") paint(active);
            return value;
          });
          // Speculative failures are shown only if this direction is committed.
          promise.catch(() => {});
          active.pending.set(direction, promise);
        }
      }
    }

    function paint(active) {
      if (!track) return;
      const direction = active.dx < 0 ? 1 : -1;
      const adjacent = direction > 0 ? active.next : active.previous;
      const displacement = adjacent
        ? Math.max(-active.width, Math.min(active.width, active.dx))
        : Math.sign(active.dx) * Math.min(Math.abs(active.dx) * 0.28, active.width * 0.22);
      track.style.transform = `translate3d(${displacement}px, 0, 0)`;
    }

    function animateTo(displacement, duration) {
      if (!track || reducedMotion.matches || !duration) {
        if (track) track.style.transform = `translate3d(${displacement}px, 0, 0)`;
        return Promise.resolve();
      }
      const movingTrack = track;
      return new Promise((resolve) => {
        let timer;
        const done = () => {
          window.clearTimeout(timer);
          movingTrack.removeEventListener("transitionend", ended);
          if (finishAnimation === done) finishAnimation = null;
          resolve();
        };
        const ended = (event) => {
          if (event.target === movingTrack && event.propertyName === "transform") done();
        };
        finishAnimation = done;
        movingTrack.addEventListener("transitionend", ended);
        movingTrack.getBoundingClientRect();
        movingTrack.style.transition = `transform ${duration}ms cubic-bezier(.2, .7, .2, 1)`;
        movingTrack.style.transform = `translate3d(${displacement}px, 0, 0)`;
        timer = window.setTimeout(done, duration + 60);
      });
    }

    async function settle(commit, direction) {
      const active = gesture;
      if (!active || phase !== "dragging") return;
      setPhase("settling");
      suppressClick();
      let adjacent = direction > 0 ? active.next : active.previous;
      if (commit && active.pending?.has(direction)) {
        try { adjacent = await active.pending.get(direction); }
        catch (error) {
          if (!disposed && gesture === active) frame.dispatchEvent(new CustomEvent("detail-swipe-error", { detail: { error } }));
          commit = false;
        }
      }
      if (disposed || gesture !== active || !frame.isConnected) return;
      commit = commit && !!adjacent;
      await animateTo(commit ? -direction * active.width : 0, commit ? 220 : 180);
      if (disposed || gesture !== active || !frame.isConnected) return;
      if (!commit || !mobile.matches) {
        finish();
        return;
      }
      setPhase("navigating");
      try {
        const navigated = await hooks?.navigate?.(direction, adjacent);
        if (navigated === false) {
          await animateTo(0, 180);
          if (!disposed && gesture === active) finish();
          return;
        }
      } catch (error) {
        if (!disposed) frame.dispatchEvent(new CustomEvent("detail-swipe-error", { detail: { error } }));
        if (!disposed && gesture === active) finish();
        return;
      }
      if (disposed || gesture !== active || !frame.isConnected) return;
      setPhase("handoff");
      // Rasterize the new main image beneath the landed pane before exposing its layer.
      frame.classList.add("is-detail-handoff");
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      suppressClick();
      if (!disposed && gesture === active) finish(true, direction);
    }

    function cancel(immediate = false) {
      if (!gesture) return;
      if (phase === "dragging" && !immediate) {
        void settle(false, 0);
        return;
      }
      if (phase !== "tracking") suppressClick();
      finish();
    }

    function touchStart(event) {
      if (disposed || !mobile.matches || !frame.isConnected) return;
      if (event.touches.length !== 1) { cancel(); return; }
      if (phase !== "idle") return;
      if (event.target instanceof Element && event.target.closest("button, a, input, select, textarea, .detail-filmstrip, [data-detail-dot]")) return;
      const touch = event.touches[0];
      gesture = {
        id: touch.identifier, x: touch.clientX, y: touch.clientY,
        dx: 0, dy: 0, width: frame.clientWidth, height: frame.clientHeight,
        previous: null, next: null,
        samples: [{ x: touch.clientX, time: event.timeStamp }],
        minX: touch.clientX, maxX: touch.clientX,
      };
      setPhase("tracking");
    }

    function touchMove(event) {
      const active = gesture;
      if (!active || (phase !== "tracking" && phase !== "dragging")) return;
      if (event.touches.length !== 1 || !mobile.matches) { cancel(); return; }
      const touch = Array.from(event.touches).find((item) => item.identifier === active.id);
      if (!touch) { cancel(); return; }
      active.dx = touch.clientX - active.x;
      active.dy = touch.clientY - active.y;
      sampleMovement(active, touch.clientX, event.timeStamp);
      if (phase === "tracking") {
        if (Math.abs(active.dy) >= 8 && Math.abs(active.dy) >= Math.abs(active.dx) * 0.9) {
          cancel(true);
          return;
        }
        if (Math.abs(active.dx) < 10 || Math.abs(active.dx) < Math.abs(active.dy) * 1.2) return;
        setPhase("dragging");
        try { createOverlay(active); }
        catch (error) {
          frame.dispatchEvent(new CustomEvent("detail-swipe-error", { detail: { error } }));
          cancel(true);
          return;
        }
      }
      if (event.cancelable) event.preventDefault();
      paint(active);
    }

    function touchEnd(event) {
      const active = gesture;
      if (!active) return;
      if (phase === "tracking") { finish(); return; }
      if (phase !== "dragging") return;
      if (event.touches.length) { cancel(); return; }
      const touch = Array.from(event.changedTouches).find((item) => item.identifier === active.id);
      if (!touch) { cancel(); return; }
      active.dx = touch.clientX - active.x;
      active.dy = touch.clientY - active.y;
      // Include release time so a paused finger cannot retain an earlier flick velocity.
      sampleMovement(active, touch.clientX, event.timeStamp);
      const threshold = Math.max(48, Math.min(80, active.width * 0.2));
      const velocity = releaseVelocity(active);
      const direction = Math.sign(active.dx);
      // Tolerate release jitter without treating an intentional drag back as a flick.
      const reversedDistance = direction < 0 ? touch.clientX - active.minX : active.maxX - touch.clientX;
      const flick = Math.abs(active.dx) >= flickDistance
        && Math.abs(velocity) >= flickVelocity
        && Math.sign(velocity) === direction && reversedDistance <= 6;
      const commit = (Math.abs(active.dx) >= threshold || flick)
        && Math.abs(active.dx) >= Math.abs(active.dy) * 1.15;
      if (event.cancelable) event.preventDefault();
      void settle(commit, active.dx < 0 ? 1 : -1);
    }

    function suppressCapturedClick(event) {
      if (Date.now() >= suppressClickUntil) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }

    function resized() {
      if (gesture && (Math.abs(frame.clientWidth - gesture.width) > 1 || Math.abs(frame.clientHeight - gesture.height) > 1 || !mobile.matches)) cancel(true);
    }

    const touchCancel = () => cancel();
    const hidden = () => { if (document.hidden) cancel(true); };
    const observer = new MutationObserver(() => { if (!frame.isConnected) cleanup(); });
    const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(resized) : null;

    function cleanup() {
      if (disposed) return;
      disposed = true;
      cancel(true);
      removeOverlay();
      observer.disconnect();
      resizeObserver?.disconnect();
      frame.removeEventListener("touchstart", touchStart);
      frame.removeEventListener("touchmove", touchMove);
      frame.removeEventListener("touchend", touchEnd);
      frame.removeEventListener("touchcancel", touchCancel);
      frame.removeEventListener("click", suppressCapturedClick, true);
      window.removeEventListener("resize", resized);
      document.removeEventListener("visibilitychange", hidden);
      if (bindings.get(frame) === cleanup) bindings.delete(frame);
    }

    frame.addEventListener("touchstart", touchStart, { passive: true });
    frame.addEventListener("touchmove", touchMove, { passive: false });
    frame.addEventListener("touchend", touchEnd, { passive: false });
    frame.addEventListener("touchcancel", touchCancel, { passive: true });
    frame.addEventListener("click", suppressCapturedClick, true);
    window.addEventListener("resize", resized, { passive: true });
    document.addEventListener("visibilitychange", hidden);
    observer.observe(document.documentElement, { childList: true, subtree: true });
    resizeObserver?.observe(frame);
    bindings.set(frame, cleanup);
    return cleanup;
  }

  window.ImageStudioDetailSwipe = { bind };
})();
