(function () {
  "use strict";

  // Match the generation-detail dialog. Keep the DOM and scroll lock alive
  // through exit, and never let an interrupted exit hide a newer dialog.
  const jobs = new WeakMap();
  const duration = 180;

  function targets(root) {
    const panel = root.querySelector(".studio-modal, .image-preview__panel");
    const backdrop = root.querySelector(".studio-modal-scrim, .image-preview__backdrop");
    return panel ? [[panel, true], [backdrop, false]].filter(([element]) => element) : [[root, !root.classList.contains("scrim")]];
  }

  function cancel(root) {
    const job = jobs.get(root);
    jobs.delete(root);
    job?.animations.forEach(animation => animation.cancel());
  }

  function run(root, closing, complete) {
    const previous = jobs.get(root);
    const parts = targets(root).map(([element, scale]) => {
      const style = getComputedStyle(element);
      return { element, scale, current: previous ? { opacity: style.opacity, transform: style.transform } : null };
    });
    cancel(root);
    root.classList.remove("is-hidden");
    root.classList.toggle("is-closing", closing);
    const job = { animations: [] };
    jobs.set(root, job);
    const finish = () => {
      if (jobs.get(root) !== job) return;
      if (closing) root.classList.add("is-hidden");
      root.classList.remove("is-closing");
      cancel(root);
      complete?.();
    };
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !root.animate) { finish(); return; }
    for (const { element, scale, current } of parts) {
      const style = getComputedStyle(element);
      const base = style.transform === "none" ? "" : style.transform;
      const visible = { opacity: style.opacity, ...(scale ? { transform: `${base} scale(1)` } : {}) };
      const hidden = { opacity: 0, ...(scale ? { transform: `${base} scale(.985)` } : {}) };
      const start = current ? { opacity: current.opacity, ...(scale ? { transform: current.transform } : {}) } : closing ? visible : hidden;
      job.animations.push(element.animate([start, closing ? hidden : visible], { duration, easing: "ease", fill: "both" }));
    }
    Promise.all(job.animations.map(animation => animation.finished)).then(finish, () => {});
  }

  window.ImageStudioDialogMotion = {
    show(root) {
      if (!root.classList.contains("is-hidden") && !root.classList.contains("is-closing")) return;
      run(root, false);
    },
    hide(root, complete) {
      if (root.classList.contains("is-hidden")) { complete?.(); return; }
      if (!root.classList.contains("is-closing")) run(root, true, complete);
    },
    hideImmediately(root) {
      cancel(root);
      root.classList.remove("is-closing");
      root.classList.add("is-hidden");
    },
  };
})();
