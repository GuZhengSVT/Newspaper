(() => {
  'use strict';
  const root = document.documentElement;
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const themeButton = document.querySelector('#theme');
  let savedTheme = null;
  try { savedTheme = localStorage.getItem('newspaper-theme'); } catch {}
  function applyTheme(theme) {
    root.dataset.theme = theme;
    if (themeButton) {
      themeButton.textContent = theme === 'dark' ? '浅色' : '深色';
      themeButton.setAttribute('aria-label', theme === 'dark' ? '切换为浅色配色' : '切换为深色配色');
    }
  }
  applyTheme(['light', 'dark'].includes(savedTheme) ? savedTheme : systemDark.matches ? 'dark' : 'light');
  systemDark.addEventListener('change', event => { if (!savedTheme) applyTheme(event.matches ? 'dark' : 'light'); });
  themeButton?.addEventListener('click', () => {
    savedTheme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(savedTheme);
    try { localStorage.setItem('newspaper-theme', savedTheme); } catch {}
  });

  const contents = document.querySelector('#contents-panel');
  const narrow = matchMedia('(max-width: 1279px)');
  if (contents) {
    contents.open = !narrow.matches;
    narrow.addEventListener('change', () => { contents.open = !narrow.matches; });
    const links = [...contents.querySelectorAll('a[href^="#"]')];
    function highlight(id) {
      links.forEach(link => {
        if (link.hash === '#' + id) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }
    contents.addEventListener('click', event => {
      const link = event.target.closest('a[href^="#"]');
      if (!link) return;
      const target = document.getElementById(link.hash.slice(1));
      if (!target) return;
      highlight(target.id);
      if (narrow.matches) {
        event.preventDefault();
        contents.open = false;
        history.replaceState(null, '', link.hash);
        target.scrollIntoView({block: 'start'});
      }
    });
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver(entries => {
        for (const entry of entries) if (entry.isIntersecting) highlight(entry.target.id);
      }, {rootMargin: '-60px 0px -65% 0px'});
      document.querySelectorAll('[data-outline]').forEach(element => observer.observe(element));
    }
  }

  const input = document.querySelector('#search');
  let indexPromise;
  input?.addEventListener('input', async () => {
    try {
      indexPromise ||= fetch('/search.json', {cache: 'no-store'}).then(response => {
        if (!response.ok) throw Error('search');
        return response.json();
      });
      const index = await indexPromise;
      const query = input.value.toLocaleLowerCase().trim();
      const matches = new Set(index.filter(item => (item.title + ' ' + item.text).toLocaleLowerCase().includes(query)).map(item => item.date));
      let visible = 0;
      document.querySelectorAll('.archive-entry').forEach(row => {
        row.hidden = query !== '' && !matches.has(row.dataset.key);
        if (!row.hidden) visible++;
      });
      document.querySelector('#search-empty').hidden = visible !== 0;
    } catch { indexPromise = null; }
  });
})();
