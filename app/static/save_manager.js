(() => {
  const tabs = Array.from(document.querySelectorAll('.sb-save-tabs [role="tab"]'));
  if (!tabs.length) return;
  const activate = (tab) => {
    tabs.forEach((t) => {
      const selected = t === tab;
      t.setAttribute('aria-selected', String(selected));
      document.getElementById(t.getAttribute('aria-controls')).hidden = !selected;
    });
  };
  tabs.forEach((tab) => tab.addEventListener('click', () => activate(tab)));
})();
