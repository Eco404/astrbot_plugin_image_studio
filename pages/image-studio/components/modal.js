(function () {
  "use strict";

  // Own dialog state, focus restoration and listener lifetime independently of views.
  window.ImageStudioModal = function ({ escape, showNotice, errorMessage, syncPageScrollLock }) {
    const $ = id => document.getElementById(id);
    const { renderIcons } = window.ImageStudioPresentation;
    let modalClose = null;
    let modalDismissOutside = true;
    let listeners = null;

    async function copyText(content, success = "已复制到剪贴板。") {
      let copied = false;
      try { await navigator.clipboard.writeText(content); copied = true; } catch { /* The host iframe may deny clipboard permissions. */ }
      if (!copied) {
        const focus = document.activeElement;
        const input = document.createElement("textarea");
        input.value = content; input.className = "clipboard-buffer";
        (modalClose ? $("studioModal") : document.body).appendChild(input);
        input.select();
        try { copied = document.execCommand("copy"); } catch { copied = false; }
        input.remove(); focus?.focus?.({ preventScroll: true });
      }
      if (copied) showNotice(success, "success");
      else {
        await openModal("复制参数", `<label class="field">参数内容<textarea id="manualCopyValue" rows="12" readonly>${escape(content)}</textarea></label>`, [{ label: "关闭", action: () => true }]);
      }
      return copied;
    }

    function openModal(title, body, actions, options = {}) {
      if (modalClose) modalClose(false, true);
      const previousFocus = document.activeElement;
      const root = $("studioModalRoot"), modal = $("studioModal");
      $("studioModalTitle").textContent = title;
      $("studioModalBody").innerHTML = body;
      $("studioModalError").textContent = "";
      $("studioModalFooter").innerHTML = "";
      modal.removeAttribute("aria-busy");
      $("studioModalBody").scrollTop = 0;
      $("studioModal").classList.toggle("is-merge-picker", !!options.mergePicker);
      $("studioModal").classList.toggle("is-import-editor", !!options.importEditor);
      $("studioModal").classList.toggle("is-external-editor", !!options.externalEditor);
      modalDismissOutside = options.dismissOutside !== false;
      window.ImageStudioDialogMotion.show(root);
      syncPageScrollLock();
      return new Promise((resolve) => {
        let pending = false, closing = false;
        const close = (result, immediate = false) => {
          if (modalClose !== close || (!immediate && (pending || closing))) return;
          window.ImageStudioSelect?.close();
          if (!closing) options.onClose?.();
          closing = true;
          const finish = () => {
            if (modalClose !== close) return;
            modalClose = null;
            if (!immediate) {
              syncPageScrollLock();
              if (previousFocus?.isConnected) previousFocus.focus?.({ preventScroll: true });
            }
            resolve(result);
          };
          if (immediate) { window.ImageStudioDialogMotion.hideImmediately(root); finish(); }
          else window.ImageStudioDialogMotion.hide(root, finish);
        };
        modalClose = close;
        for (const definition of actions) {
          const button = document.createElement("button");
          button.type = "button"; button.className = definition.danger ? "danger-button" : definition.primary ? "primary-button" : "quiet-button";
          button.textContent = definition.label;
          if (definition.id) button.id = definition.id;
          button.disabled = typeof definition.disabled === "function" ? definition.disabled() : !!definition.disabled;
          button.addEventListener("click", async () => {
            if (modalClose !== close || pending || closing) return;
            pending = true; modal.setAttribute("aria-busy", "true");
            $("studioModalFooter").querySelectorAll("button").forEach((item) => { item.disabled = true; });
            try { const result = await definition.action(); pending = false; if (result !== undefined) close(result); }
            catch (error) { if (modalClose === close) $("studioModalError").textContent = errorMessage(error, "操作失败"); }
            finally {
              pending = false;
              if (modalClose === close) { modal.removeAttribute("aria-busy"); $("studioModalFooter").querySelectorAll("button").forEach((item, index) => { const disabled = actions[index]?.disabled; item.disabled = typeof disabled === "function" ? disabled() : !!disabled; }); }
            }
          });
          $("studioModalFooter").appendChild(button);
        }
        renderIcons($("studioModal"));
        options.onOpen?.();
        window.ImageStudioSelect?.refresh($("studioModal"));
        (options.focus ? $(options.focus) : $("studioModalFooter").querySelector("button"))?.focus({ preventScroll: true });
      });
    }

    function bind() {
      if (listeners) return;
      listeners = new AbortController();
      const { signal } = listeners;
      $("studioModalClose").addEventListener("click", () => modalClose?.(false), { signal });
      $("studioModalRoot").querySelector(".studio-modal-scrim").addEventListener("click", () => { if (modalDismissOutside) modalClose?.(false); }, { signal });
      document.addEventListener("keydown", (event) => {
        if (document.querySelector(".pswp--open")) return;
        if (modalClose && event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); modalClose(false); return; }
        if (event.key !== "Tab") return;
        const modal = modalClose ? $("studioModal") : !$("confirmDialog").classList.contains("is-hidden") ? $("confirmDialog") : !$("parameterDialog").classList.contains("is-hidden") ? $("parameterDialog") : !$("imagePreview").classList.contains("is-hidden") ? $("imagePreview") : $("detailDrawer").classList.contains("is-open") ? $("detailDrawer") : null;
        if (!modal) return;
        const focusable = Array.from(modal.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')).filter((item) => !item.matches(".studio-select-native") && !item.closest("[inert]") && item.getClientRects().length);
        if (!focusable.length) { event.preventDefault(); modal.focus(); return; }
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }, { capture: true, signal });
    }

    function dispose() {
      modalClose?.(false, true);
      listeners?.abort(); listeners = null;
    }

    return { bind, dispose, openModal, copyText, isOpen: () => !!modalClose };
  };
})();
