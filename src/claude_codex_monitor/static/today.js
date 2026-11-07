/* Today: what needs you from every source on one page, plus the tabs that source plugins add.
   Uses the page's helpers (t, tm, esc, getJson, toast, fullLabel, dur, render). */
window.CCMTabs = (() => {
  const tabs = new Map();
  return {register(name, impl) { tabs.set(name, impl); if (window.render) window.render(); }, get: name => tabs.get(name)};
})();

window.Today = (() => {
  let data = null, fetched = 0, pending = null, error = null;
  const loaded = new Set(), busy = new Set();
  const ACT_KINDS = ['reply', 'approve', 'deliver', 'alert'];        // you have to do something
  const kindName = k => t('today.kind.' + k);
  const ago = ts => ts ? t('time.ago', dur(Math.max(0, Date.now() / 1000 - ts))) : '';
  const isOpen = item => item.open && !item.handled;
  const SHOW = 8;                                     // per column; the rest is one click away
  // Sessions: the same last 3 days the session list shows by default; older waits are usually abandoned
  const agents = () => data ? data.agents.filter(a => (a.since || 0) >= midnight(2)) : [];

  function route(hash) {
    if (hash === '#/today') return {name: 'today'};
    const m = (hash || '').match(/^#\/x\/([a-z][a-z0-9-]{0,31})(?:\/(.*))?$/);
    return m ? {name: 'ext', ext: m[1], rest: m[2] || ''} : null;
  }
  function needsYou() {
    if (!data) return 0;
    return agents().length + data.schedules.length +
      data.sources.reduce((n, s) => n + s.items.filter(i => isOpen(i) && ACT_KINDS.includes(i.kind)).length, 0);
  }
  function badge() {
    const n = needsYou();
    return n ? `<span class="badge" title="${t('today.badge')}">${n}</span>` : '';
  }
  function extLinks(active) {
    if (!data) return '';
    return data.sources.filter(s => s.tab).map(s => {
      const on = active && active.name === 'ext' && active.ext === s.name;
      const n = s.items.filter(i => isOpen(i) && ACT_KINDS.includes(i.kind)).length;
      return `<a href="#/x/${esc(s.name)}" class="${on ? 'on' : ''}"${on ? ' aria-current="page"' : ''}>${esc(label(s))}${n ? `<span class="badge">${n}</span>` : ''}</a>`;
    }).join('');
  }
  function label(s) {
    const l = s.label || {};
    return l[document.documentElement.lang === 'zh' ? 'zh' : 'en'] || l.en || s.name;
  }
  async function refresh(force) {
    if (pending) return pending;
    if (!force && data && Date.now() - fetched < 3000) return;
    pending = getJson('/api/today').then(value => {
      data = value; fetched = Date.now(); error = null;
      for (const s of data.sources) {
        if (s.tab && s.tab_script && !loaded.has(s.name)) {      // the tab's own script, served by the monitor
          loaded.add(s.name);
          const el = document.createElement('script');
          el.src = '/ext/' + s.name + '/' + s.tab_script;
          document.head.appendChild(el);
        }
      }
    }).catch(e => { error = e.userMessage || e.message; }).finally(() => { pending = null; });
    return pending;
  }
  function windowLabel() {
    if (!data) return '';
    return t('today.window', fullLabel(data.window.start), fullLabel(data.window.end));
  }

  // ---- one line per thing that needs you ----
  function line(href, chip, title, sub, actions) {
    return `<div class="tline">${chip}<span class="tmain">${href ? `<a href="${href}">${esc(title)}</a>` : `<b>${esc(title)}</b>`}
      ${sub ? `<span class="tsub">${sub}</span>` : ''}</span>${actions || ''}</div>`;
  }
  function itemLine(item) {
    const sub = [item.who, item.where, item.due ? t('today.due', fullLabel(item.due)) : ago(item.since)].filter(Boolean).map(esc).join(' · ');
    const acts = item.handled ? '' : `<span class="tacts">${['done', 'later', 'skip'].map(a =>
      `<button class="tbtn${a === 'done' ? ' pri' : ''}" data-today-act="${a}" data-today-key="${esc(item.key)}"${busy.has(item.key) ? ' disabled' : ''}>${t('today.act.' + (a === 'done' && item.kind === 'waiting' ? 'received' : a))}</button>`).join('')}</span>`;
    return line(item.url, `<span class="tk k-${item.kind}">${esc(kindName(item.kind))}</span>`, item.title || item.text, sub, acts);
  }
  function column(title, lines, more) {
    const rest = lines.length - SHOW;
    return `<section class="tcol"><h3><span>${title}</span><span class="faint">${lines.length}</span></h3>${lines.length ? lines.slice(0, SHOW).join('') : `<div class="tempty">${t('today.empty')}</div>`}` +
      (rest > 0 ? `<div class="tempty">${t('today.more', rest)}</div>` : '') + (more || '') + '</section>';
  }
  function renderToday() {
    if (error && !data) return `<div class="empty">${esc(tm(error))}</div>`;
    if (!data) return `<div class="loading">${t('loading.detail')}</div>`;
    const sessions = agents().sort((a, b) => (b.since || 0) - (a.since || 0)).map(a => line('#/s/' + encodeURIComponent(a.key),
      `<span class="cl c-${esc(a.client)}">${a.client === 'claude' ? 'Claude' : 'Codex'}</span>`, a.title, esc(tm(a.text) || ago(a.since))));
    const tasks = data.schedules.map(s => line('#/schedules/' + s.source + '/' + encodeURIComponent(s.id),
      `<span class="cl c-${esc(s.source)}">${esc(t('schedule.source.' + s.source))}</span>`, s.title,
      esc(t('schedule.result.' + s.status) + (s.summary ? ' · ' + tm(s.summary) : ''))));
    let cols = column(t('today.agents'), sessions, `<a class="tmore" href="#/">${t('today.go', t('schedule.sessions'))}</a>`) +
      column(t('today.schedules'), tasks, `<a class="tmore" href="#/schedules">${t('today.go', t('schedule.title'))}</a>`);
    const others = [];
    for (const s of data.sources) {
      const open = s.items.filter(isOpen);
      const act = open.filter(i => ACT_KINDS.includes(i.kind));
      others.push(...open.filter(i => !ACT_KINDS.includes(i.kind) && (i.kind !== 'info' || (i.since || 0) >= data.window.start)));
      const err = s.error ? `<div class="terr">${esc(t('today.source_error', label(s), s.error))}</div>` : '';
      cols += column(esc(label(s)) + ' · ' + t('today.needs_you'), act.map(itemLine),
        err + (s.tab ? `<a class="tmore" href="#/x/${esc(s.name)}">${t('today.go', esc(label(s)))}</a>` : ''));
    }
    if (data.sources.length) cols += column(t('today.waiting_info'), others.map(itemLine));
    const errs = (data.source_errors || []).map(e => `<div class="terr">${esc(t('today.source_error', e.name, e.error))}</div>`).join('');
    return `<div class="tday"><p class="tlead">${t('today.lead', needsYou())}</p>${errs}<div class="tgrid">${cols}</div></div>`;
  }
  function renderExt(rt) {
    const s = data && data.sources.find(x => x.name === rt.ext);
    const tab = window.CCMTabs.get(rt.ext);
    if (!s) return `<div class="empty">${data ? t('today.ext_missing') : t('loading.detail')}</div>`;
    if (!tab) return `<div class="loading">${t('loading.detail')}</div>`;
    try {
      return tab.render({source: s, route: rt, act: itemAction, itemLine, api: (path, opt) => getJson('/api/ext/' + rt.ext + '/' + path, opt)});
    } catch (e) { return `<div class="terr">${esc(String(e))}</div>`; }
  }
  function render(rt) { return rt.name === 'ext' ? renderExt(rt) : renderToday(); }

  async function itemAction(key, action) {
    let until;
    if (action === 'later') {               // later: the next 9:00 in the morning
      const d = new Date(); d.setSeconds(0, 0);
      if (d.getHours() >= 9) d.setDate(d.getDate() + 1);
      d.setHours(9, 0, 0, 0);
      until = d.getTime() / 1000;
    }
    busy.add(key); window.render();
    try {
      await getJson('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action, until})});
      await refresh(true);
      toast(() => t('today.toast.' + action), action === 'restore' ? null : async () => {
        try { await getJson('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action: 'restore'})}); await refresh(true); window.render(); }
        catch (e) { toast(e.userMessage || e.message); }
      });
    } catch (e) { toast(e.userMessage || e.message); }
    finally { busy.delete(key); window.render(); }
  }
  function click(event) {
    const b = event.target.closest('[data-today-act]');
    if (b) { itemAction(b.dataset.todayKey, b.dataset.todayAct); return true; }
    const rt = route(location.hash);
    const tab = rt && rt.name === 'ext' && window.CCMTabs.get(rt.ext);
    if (tab && tab.click) return !!tab.click(event, {act: itemAction, api: (path, opt) => getJson('/api/ext/' + rt.ext + '/' + path, opt)});
    return false;
  }
  return {route, refresh, render, click, badge, extLinks, windowLabel, needsYou};
})();
