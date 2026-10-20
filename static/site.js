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

  const home = document.querySelector('#homepage');
  if (home) {
    const controls = home.querySelector('.home-controls');
    const tablist = home.querySelector('.home-tabs');
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    const panels = tabs.map(tab => document.getElementById(tab.getAttribute('aria-controls')));
    const layoutSelect = home.querySelector('#home-layout');
    let active = 0;
    let layout = home.dataset.layout;
    if (home.dataset.layoutSwitch === 'true') {
      try {
        const saved = localStorage.getItem('newspaper-home-layout');
        if (['tabs', 'columns'].includes(saved)) layout = saved;
      } catch {}
    }
    home.style.setProperty('--home-columns', Math.min(3, Math.max(1, panels.length)));
    home.dataset.enhanced = 'true';
    function renderHome() {
      home.dataset.layout = layout;
      controls.hidden = layout === 'columns' && !layoutSelect;
      tablist.hidden = layout !== 'tabs';
      if (layoutSelect) layoutSelect.value = layout;
      panels.forEach((panel, index) => {
        const selected = index === active;
        panel.hidden = layout === 'tabs' && !selected;
        tabs[index].setAttribute('aria-selected', String(selected));
        tabs[index].tabIndex = selected ? 0 : -1;
        if (layout === 'tabs') {
          panel.setAttribute('role', 'tabpanel');
          panel.setAttribute('aria-labelledby', tabs[index].id);
          panel.tabIndex = 0;
        } else {
          panel.removeAttribute('role');
          panel.removeAttribute('aria-labelledby');
          panel.removeAttribute('tabindex');
        }
        const outline = home.querySelector(`.toc-edition[href="#${panel.id}"]`);
        if (outline?.nextElementSibling) outline.nextElementSibling.hidden = layout === 'tabs' && !selected;
      });
    }
    function selectTarget(hash) {
      const target = document.getElementById(hash.slice(1));
      const index = panels.findIndex(panel => panel === target || panel.contains(target));
      if (index < 0) return;
      active = index;
      renderHome();
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => { active = index; renderHome(); });
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabs.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        active = next;
        renderHome();
        tabs[next].focus();
      });
    });
    layoutSelect?.addEventListener('change', () => {
      layout = layoutSelect.value;
      renderHome();
      try { localStorage.setItem('newspaper-home-layout', layout); } catch {}
    });
    // Activate a hidden panel before the directory's scrolling handler runs.
    home.addEventListener('click', event => {
      const link = event.target.closest('a[href^="#"]');
      if (link) selectTarget(link.hash);
    }, true);
    window.addEventListener('hashchange', () => {
      selectTarget(location.hash);
      document.getElementById(location.hash.slice(1))?.scrollIntoView({block: 'start'});
    });
    renderHome();
    selectTarget(location.hash);
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({block: 'start'});
  }

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
