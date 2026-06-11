// Set the theme class before React mounts to avoid a flash of the wrong theme.
// Kept as an external file (not inline) so the strict CSP `script-src 'self'`
// covers it without needing 'unsafe-inline' or a per-build hash.
(function () {
  try {
    var stored = localStorage.getItem('medanon_theme');
    var resolved = stored;
    if (!resolved || resolved === 'system') {
      resolved = window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light';
    }
    if (resolved === 'dark') document.documentElement.classList.add('dark');
    document.documentElement.style.colorScheme = resolved;
  } catch (e) {
    /* ignore */
  }
})();
