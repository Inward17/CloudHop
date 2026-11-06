// Applies the saved theme before the page paints, so it never flashes the wrong colors.
// "system" (the default) follows the device setting.
(() => {
  let theme = 'system';
  try { theme = localStorage.getItem('cloudhop-theme') || 'system'; } catch { /* Storage blocked: follow the device. */ }
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
})();
