(() => {
  "use strict";

  function create() {
    const placeholder = document.createElement("div");
    placeholder.className = "image-studio-image-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    const icon = window.StudioIcons.createElement(window.StudioIcons.Image);
    icon.setAttribute("viewBox", "2 2 20 20");
    icon.setAttribute("stroke-width", "8");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    // Keep the same crisp stroke at every viewing size.
    icon.querySelectorAll("path,rect,circle,line,polyline,polygon,ellipse").forEach(shape => shape.setAttribute("vector-effect", "non-scaling-stroke"));
    icon.classList.add("image-studio-placeholder-icon");
    placeholder.appendChild(icon);
    return placeholder;
  }

  window.ImageStudioImagePlaceholder = { create };
})();
