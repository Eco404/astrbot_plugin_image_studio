(() => {
  "use strict";

  const states = new WeakMap();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function sourceKey(source) {
    try { return new URL(source, document.baseURI).href; }
    catch { return source; }
  }

  function updatePhase(state) {
    state.image.dataset.backdropState = state.paused ? "paused" : state.pending
      ? (state.pending.fading ? "fading" : "loading")
      : state.fade ? "fading" : "idle";
  }

  function cancelPending(state) {
    const request = state.pending;
    if (!request) return;
    state.pending = null;
    request.cancel();
    request.resolve(false);
    request.candidate?.removeAttribute("src");
    updatePhase(state);
  }

  function restoreProperty(style, name, previous) {
    if (previous.value) style.setProperty(name, previous.value, previous.priority);
    else style.removeProperty(name);
  }

  function fadeTo(state, source, opacity, previousClass) {
    const image = state.image;
    const oldSource = image.getAttribute("src");
    const oldOpacity = Number.parseFloat(window.getComputedStyle(image).opacity);
    const previous = oldSource && image.complete && image.naturalWidth > 0
      ? image.cloneNode(false) : null;
    const properties = Object.fromEntries(["animation", "transition"].map((name) => [
      name, { value: image.style.getPropertyValue(name), priority: image.style.getPropertyPriority(name) },
    ]));

    // CSS motion overrides must not shorten this decode-gated, opacity-only transition.
    image.style.setProperty("animation", "none", "important");
    image.style.setProperty("transition", "none", "important");
    if (previous) {
      previous.removeAttribute("id");
      previous.removeAttribute("data-backdrop-state");
      previous.classList.add(...previousClass.split(/\s+/).filter(Boolean));
      previous.setAttribute("aria-hidden", "true");
      previous.style.setProperty("animation", "none", "important");
      previous.style.setProperty("transition", "none", "important");
      previous.style.opacity = String(Number.isFinite(oldOpacity) ? oldOpacity : opacity);
      image.before(previous);
    }
    image.src = source;
    image.style.opacity = String(opacity);

    let resolve;
    let finished = false;
    const animations = [];
    const fade = {
      promise: new Promise((done) => { resolve = done; }),
      pause() {
        for (const animation of animations) {
          const position = animation.currentTime;
          animation.pause();
          if (position !== null) animation.currentTime = position;
        }
      },
      resume() {
        if (!finished) for (const animation of animations) animation.play();
      },
      finish() {
        if (finished) return;
        finished = true;
        image.style.opacity = String(opacity);
        for (const animation of animations) animation.cancel();
        previous?.remove();
        restoreProperty(image.style, "animation", properties.animation);
        restoreProperty(image.style, "transition", properties.transition);
        if (state.fade === fade) state.fade = null;
        updatePhase(state);
        resolve();
      },
    };
    state.fade = fade;
    if (typeof image.animate !== "function") {
      fade.finish();
      return fade.promise;
    }
    try {
      const options = {
        duration: reducedMotion.matches ? 140 : 280,
        easing: "ease-in-out",
        fill: "both",
      };
      animations.push(image.animate([{ opacity: 0 }, { opacity }], options));
      if (previous) {
        animations.push(previous.animate([
          { opacity: Number.isFinite(oldOpacity) ? oldOpacity : opacity },
          { opacity: 0 },
        ], options));
      }
      Promise.all(animations.map((animation) => animation.finished.catch(() => {}))).then(fade.finish);
    } catch {
      for (const animation of animations) animation.finished.catch(() => {});
      fade.finish();
    }
    return fade.promise;
  }

  function makeState(image) {
    const state = { image, pending: null, fade: null, observer: null, disposed: false, paused: false };
    state.observer = new MutationObserver(() => {
      if (!image.isConnected) dispose(image);
    });
    state.observer.observe(document.documentElement, { childList: true, subtree: true });
    states.set(image, state);
    return state;
  }

  function transition(image, source, options = {}) {
    if (!(image instanceof HTMLImageElement)) return Promise.resolve(false);
    if (!options || typeof options !== "object") options = {};
    const state = states.get(image) || (image.isConnected ? makeState(image) : null);
    if (!state || state.disposed) return Promise.resolve(false);
    if (state.paused) {
      state.paused = false;
      state.fade?.resume();
      updatePhase(state);
    }
    const nextSource = typeof source === "string" ? source.trim() : "";
    if (!nextSource) {
      cancelPending(state);
      return Promise.resolve(false);
    }
    const key = sourceKey(nextSource);
    if (state.pending?.key === key) return state.pending.promise;
    cancelPending(state);

    let resolve;
    let cancel;
    const cancelled = new Promise((done) => { cancel = () => done(false); });
    const request = {
      key, candidate: null, fading: false, cancel,
      promise: new Promise((done) => { resolve = done; }),
      resolve: (result) => resolve(result),
    };
    state.pending = request;
    updatePhase(state);
    const current = () => !state.disposed && image.isConnected && state.pending === request;
    const opacityValue = Number(options.opacity ?? 0.62);
    const opacity = Number.isFinite(opacityValue) ? Math.max(0, Math.min(1, opacityValue)) : 0.62;
    const previousClass = typeof options.previousClass === "string" && options.previousClass.trim()
      ? options.previousClass.trim() : "detail-backdrop-previous";

    async function apply() {
      if (image.getAttribute("src") && sourceKey(image.getAttribute("src")) === key && image.complete && image.naturalWidth > 0) {
        if (state.fade) await Promise.race([state.fade.promise, cancelled]);
        return current();
      }
      const candidate = new Image();
      candidate.className = image.className;
      candidate.decoding = "async";
      if (image.crossOrigin) candidate.crossOrigin = image.crossOrigin;
      if (image.referrerPolicy) candidate.referrerPolicy = image.referrerPolicy;
      request.candidate = candidate;
      const loaded = new Promise((done) => {
        candidate.onload = () => done(candidate.naturalWidth > 0);
        candidate.onerror = () => done(false);
      });
      candidate.src = nextSource;
      let decoding;
      try {
        decoding = typeof candidate.decode === "function"
          ? candidate.decode().then(() => candidate.naturalWidth > 0, () => false)
          : loaded;
      } catch {
        decoding = Promise.resolve(false);
      }
      const ready = await Promise.race([decoding, cancelled]);
      candidate.onload = null;
      candidate.onerror = null;
      if (!ready || !current()) return false;

      // Finish the current visual blend before starting another; never stack stale layers.
      if (state.fade) await Promise.race([state.fade.promise, cancelled]);
      if (!current()) return false;
      request.fading = true;
      updatePhase(state);
      await fadeTo(state, nextSource, opacity, previousClass);
      return current();
    }

    apply().then((result) => {
      if (state.pending === request) {
        state.pending = null;
        updatePhase(state);
      }
      request.resolve(result);
    }, () => {
      if (state.pending === request) {
        state.pending = null;
        updatePhase(state);
      }
      request.resolve(false);
    });
    return request.promise;
  }

  function pause(image) {
    const state = states.get(image);
    if (!state || state.disposed) return;
    cancelPending(state);
    state.paused = true;
    state.fade?.pause();
    updatePhase(state);
  }

  function dispose(image) {
    const state = states.get(image);
    if (!state) return;
    state.disposed = true;
    cancelPending(state);
    state.fade?.finish();
    state.observer?.disconnect();
    image.dataset.backdropState = "idle";
    states.delete(image);
  }

  window.ImageStudioBackdrop = { transition, pause, dispose };
})();
