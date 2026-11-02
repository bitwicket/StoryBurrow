
(() => {
  "use strict";

  const modal = document.getElementById("sb-art-modal");
  const opener = document.getElementById("sb-art-open");

  if (!modal || !opener) return;

  const dialog = modal.querySelector(".sb-art-dialog");
  const closeButton = document.getElementById("sb-art-modal-close");
  const tabs = [...modal.querySelectorAll("[data-art-tab]")];
  const panels = [...modal.querySelectorAll("[data-art-panel]")];
  const picker = document.getElementById("sb-art-picker");

  const fileInput = document.getElementById("sb-custom-file");
  const preview = document.getElementById("sb-upload-preview");
  const previewImage = preview?.querySelector("img");

  let previousFocus = null;
  let previewURL = null;

  function selectTab(name) {
    tabs.forEach(tab => {
      const selected = tab.dataset.artTab === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });

    panels.forEach(panel => {
      panel.hidden = panel.dataset.artPanel !== name;
    });
  }

  function openModal() {
    previousFocus = document.activeElement;

    modal.hidden = false;
    document.body.classList.add("sb-art-modal-open");

    selectTab(
      tabs.some(tab => tab.dataset.artTab === "vndb")
        ? "vndb"
        : "upload"
    );

    if (picker) {
      picker.hidden = false;
    }

    dialog.focus();
  }

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove("sb-art-modal-open");

    if (previewURL) {
      URL.revokeObjectURL(previewURL);
      previewURL = null;
    }

    if (previousFocus?.isConnected) {
      previousFocus.focus();
    }
  }

  opener.addEventListener("click", openModal);
  closeButton.addEventListener("click", closeModal);

  modal.querySelectorAll(".sb-art-modal-cancel").forEach(button => {
    button.addEventListener("click", closeModal);
  });

  ["sb-art-close", "sb-art-cancel"].forEach(id => {
    document.getElementById(id)?.addEventListener(
      "click", closeModal
    );
  });

  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      selectTab(tab.dataset.artTab);
    });
  });

  modal.addEventListener("click", event => {
    if (event.target === modal) {
      closeModal();
    }
  });

  modal.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeModal();
      return;
    }

    if (event.key !== "Tab") return;

    const focusable = [...dialog.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), a[href]'
    )].filter(element =>
      element.getClientRects().length > 0
    );

    if (!focusable.length) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      document.activeElement === last
    ) {
      event.preventDefault();
      first.focus();
    }
  });

  fileInput?.addEventListener("change", () => {
    if (previewURL) {
      URL.revokeObjectURL(previewURL);
      previewURL = null;
    }

    const file = fileInput.files?.[0];
    if (!file || !preview || !previewImage) {
      if (preview) preview.hidden = true;
      return;
    }

    previewURL = URL.createObjectURL(file);
    previewImage.src = previewURL;
    preview.hidden = false;
  });
})();
