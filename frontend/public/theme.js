// Applies the theme before the page paints, so it never flashes the wrong colors: the user's choice of light or dark,
// or, until they choose, the device's setting.
(() => {
  let theme = null;
  try { theme = localStorage.getItem('cloudhop-theme'); } catch { /* Storage blocked: follow the device. */ }
  if (theme !== 'light' && theme !== 'dark') theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
})();
