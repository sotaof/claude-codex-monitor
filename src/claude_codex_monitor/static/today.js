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
  // Within a day: how long ago; older: the date and time, which is easier to place than "4 days 2 hours ago"
  const ago = ts => !ts ? '' : Date.now() / 1000 - ts < 86400 ? t('time.ago', dur(Math.max(0, Date.now() / 1000 - ts))) : md(ts) + ' ' + hm(ts);
  const isOpen = item => item.open && !item.handled;
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
    return '';             // the navigation shows no counts; each page says how many there are
  }
  function extLinks(active) {
    if (!data) return '';
    return data.sources.filter(s => s.tab).map(s => {
      const on = active && active.name === 'ext' && active.ext === s.name;
      return `<a href="#/x/${esc(s.name)}" class="${on ? 'on' : ''}"${on ? ' aria-current="page"' : ''}>${esc(label(s))}</a>`;
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
  // opts (for source tabs): href to open, kindMenu to make the kind tag a button, extra html under the line
  function itemLine(item, opts) {
    opts = opts || {};
    const sub = [item.who, item.where, item.due ? t('today.due', fullLabel(item.due)) : ago(item.since)].filter(Boolean).map(esc).join(' · ');
    const tag = opts.kindMenu ? `<button class="tk k-${item.kind} tkbtn" data-kind-menu="${esc(item.key)}" title="${t('today.change_kind')}">${esc(kindName(item.kind))} ▾</button>`
      : `<span class="tk k-${item.kind}">${esc(kindName(item.kind))}</span>`;
    const acts = item.handled ? '' : `<span class="tacts">${['done', 'skip'].map(a =>
      `<button class="tbtn${a === 'done' ? ' pri' : ''}" data-today-act="${a}" data-today-key="${esc(item.key)}"${busy.has(item.key) ? ' disabled' : ''}>${t('today.act.' + (a === 'done' && item.kind === 'waiting' ? 'received' : a))}</button>`).join('')}</span>`;
    return line(opts.href || item.url, tag, item.title || item.text, sub + (opts.extra || ''), acts);
  }
  // ---- Today: one list of what needs you, from every source; the rest folded away ----
  let order = 'old';                                  // the longest-waiting first: what to do next
  const unfolded = new Set();
  const LIST = 15;
  // The source's own buttons when it gives them, otherwise Done / Not needed
  const btnLabel = a => a.label[document.documentElement.lang === 'zh' ? 'zh' : 'en'] || a.label.en || a.label.zh || a.id;
  function actsOf(item) {
    if (!item.handled && item.actions) return `<span class="tacts">${item.actions.map(a =>
      `<button class="tbtn${a.primary ? ' pri' : ''}" data-today-custom="${esc(a.id)}" data-today-key="${esc(item.key)}"${busy.has(item.key) ? ' disabled' : ''}>${esc(btnLabel(a))}</button>`).join('')}</span>`;
    return item.handled ? '' : `<span class="tacts">${['done', 'skip'].map(a =>
      `<button class="tbtn${a === 'done' ? ' pri' : ''}" data-today-act="${a}" data-today-key="${esc(item.key)}"${busy.has(item.key) ? ' disabled' : ''}>${t('today.act.' + (a === 'done' && item.kind === 'waiting' ? 'received' : a))}</button>`).join('')}</span>`;
  }
  function trow(href, tags, title, sub, when, acts) {
    return `<div class="trow">${tags}<span class="tmain">${href ? `<a href="${href}" title="${esc(title)}">${esc(title)}</a>` : `<b>${esc(title)}</b>`}
      ${sub ? `<span class="tsub">${sub}</span>` : ''}</span><span class="twhen">${esc(when)}</span>${acts || ''}</div>`;
  }
  const srcTag = (cls, text) => `<span class="cl ${cls}">${esc(text)}</span>`;
  function sourceRow(s, i) {
    const href = s.tab ? '#/x/' + s.name + '/' + encodeURIComponent(i.key) : i.url;
    const tags = srcTag('c-ext', label(s)) + `<span class="tk k-${i.kind}">${esc(kindName(i.kind))}</span>`;
    return {since: i.since || 0, html: trow(href, tags, i.title || i.text, [i.who, i.where].filter(Boolean).map(esc).join(' · '), ago(i.since), actsOf(i))};
  }
  function section(key, title, n, rows, foldable) {
    const open = !foldable || unfolded.has(key);
    const head = foldable
      ? `<button class="tsec-h" data-today-fold="${key}" aria-expanded="${open}"><span class="faint">${open ? '▾' : '▸'}</span> <b>${title}</b> <span class="faint">${n}</span></button>`
      : `<div class="tsec-h"><b>${title}</b> <span class="faint">${n}</span><span class="sp"></span>${sortSeg('data-today-order', order, [['old', t('today.sort_longest')], ['new', t('sort.newest')]])}</div>`;
    if (!open) return `<section class="tsec">${head}</section>`;
    const sorted = rows.slice().sort((a, b) => order === 'old' ? a.since - b.since : b.since - a.since);
    const more = sorted.length > LIST && !unfolded.has(key + ':all') ? `<button class="tsec-more" data-today-fold="${key}:all">${t('today.more', sorted.length - LIST)}</button>` : '';
    const shown = unfolded.has(key + ':all') ? sorted : sorted.slice(0, LIST);
    return `<section class="tsec">${head}<div class="tlist">${shown.map(r => r.html).join('') || `<div class="tempty">${t(key === 'act' ? 'today.empty_act' : 'today.empty')}</div>`}${more}</div></section>`;
  }
  function renderToday() {
    if (error && !data) return `<div class="empty">${esc(tm(error))}</div>`;
    if (!data) return `<div class="loading">${t('loading.detail')}</div>`;
    const act = [], confirm = [], others = [];
    // Sessions close here as on the session list (a new message brings them back)
    for (const a of agents()) act.push({since: a.since || 0, html: trow('#/s/' + encodeURIComponent(a.key),
      srcTag('c-' + a.client, a.client === 'claude' ? 'Claude' : 'Codex'), a.title, esc(tm(a.text) || ''), ago(a.since),
      `<span class="tacts"><button class="tbtn" data-today-close="${esc(a.key)}" title="${esc(t('list.dismiss_hint'))}">${t('common.close')}</button></span>`)});
    for (const s of data.schedules) act.push({since: s.since || 0, html: trow('#/schedules/' + s.source + '/' + encodeURIComponent(s.id),
      srcTag('c-' + s.source, t('schedule.source.' + s.source)), s.title, esc(t('schedule.result.' + s.status) + (s.summary ? ' · ' + tm(s.summary) : '')),
      ago(s.since), `<span class="tacts"><a class="tbtn" href="#/schedules/${s.source}/${encodeURIComponent(s.id)}">${t('today.go_fix')} ›</a></span>`)});
    for (const s of data.sources) for (const i of s.items.filter(isOpen)) {
      if (ACT_KINDS.includes(i.kind)) act.push(sourceRow(s, i));
      else if (i.kind === 'confirm') confirm.push(sourceRow(s, i));
      else if (i.kind !== 'info' || (i.since || 0) >= data.window.start) others.push(sourceRow(s, i));
    }
    // One line on how each source is doing, instead of empty columns
    const status = [
      `<a href="#/">${esc(agents().length ? t('today.st.sessions', agents().length) : t('today.st.sessions_none'))}</a>`,
      `<a href="#/schedules">${esc(data.schedules.length ? t('today.st.schedules', data.schedules.length) : t('today.st.schedules_ok'))}</a>`,
      ...data.sources.map(s => `<a href="${s.tab ? '#/x/' + s.name : '#/today'}" class="${s.error ? 'bad' : ''}">${esc(s.error ? t('today.st.source_error', label(s)) : t('today.st.source', label(s), s.polled ? dur(Math.max(0, Date.now() / 1000 - s.polled)) : '—'))}</a>`),
    ].join('<span class="faint"> · </span>');
    const errs = (data.source_errors || []).concat(data.sources.filter(s => s.error).map(s => ({name: label(s), error: s.error})))
      .map(e => `<div class="terr">${esc(t('today.source_error', e.name, e.error))}</div>`).join('');
    return `<div class="tday"><p class="tlead"><b>${esc(t('today.range', windowLabel()))}</b> ${esc(t('today.summary', act.length, confirm.length, others.length))}</p>${errs}
      ${section('act', t('today.act_title'), act.length, act, false)}
      ${confirm.length ? section('confirm', t('today.confirm'), confirm.length, confirm, true) : ''}
      ${others.length ? section('others', t('today.waiting_info'), others.length, others, true) : ''}
      <div class="tstatus">${status}</div></div>`;
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
    busy.add(key); window.render();
    try {
      await getJson('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action})});
      await tell(key, action);
      await refresh(true);
      toast(() => t('today.toast.' + action), action === 'restore' ? null : async () => {
        try {
          await getJson('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action: 'restore'})});
          await tell(key, 'restore'); await refresh(true); window.render();
        } catch (e) { toast(e.userMessage || e.message); }
      });
    } catch (e) { toast(e.userMessage || e.message); }
    finally { busy.delete(key); window.render(); }
  }
  // Let the item's source know (for example to learn from "not a to-do"); its tab script may not be loaded yet
  async function tell(key, action) {
    const name = key.split(':')[0], tab = window.CCMTabs.get(name);
    if (tab && tab.onAction) {
      try { await tab.onAction(key, action, {api: (path, opt) => getJson('/api/ext/' + name + '/' + path, opt)}); }
      catch (e) { toast(e.userMessage || e.message); }
    }
  }
  async function customAction(key, action, label) {
    busy.add(key); window.render();
    const send = a => getJson('/api/item_action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action: a})});
    try {
      const r = await send(action);
      await refresh(true);
      toast(() => r.toast || label, r.undo ? async () => {
        try { await send(r.undo); await refresh(true); window.render(); } catch (e) { toast(e.userMessage || e.message); }
      } : null);
    } catch (e) { toast(e.userMessage || e.message); }
    finally { busy.delete(key); window.render(); }
  }
  async function closeSession(key) {
    try {
      await post('/api/dismiss', [key]);
      await refresh(true); window.render();
      toast(() => t('toast.closed'), async () => {
        try { await post('/api/restore', [key]); await refresh(true); window.render(); } catch (e) { toast(e.userMessage || e.message); }
      });
    } catch (e) { toast(e.userMessage || e.message); }
  }
  function click(event) {
    const cu = event.target.closest('[data-today-custom]');
    if (cu) { customAction(cu.dataset.todayKey, cu.dataset.todayCustom, cu.textContent); return true; }
    const fold = event.target.closest('[data-today-fold]');
    if (fold) { const k = fold.dataset.todayFold; unfolded.has(k) ? unfolded.delete(k) : unfolded.add(k); window.render(); return true; }
    const od = event.target.closest('[data-today-order]');
    if (od) { order = od.dataset.todayOrder; window.render(); return true; }
    const c = event.target.closest('[data-today-close]');
    if (c) { closeSession(c.dataset.todayClose); return true; }
    const b = event.target.closest('[data-today-act]');
    if (b) { itemAction(b.dataset.todayKey, b.dataset.todayAct); return true; }
    const rt = route(location.hash);
    const tab = rt && rt.name === 'ext' && window.CCMTabs.get(rt.ext);
    if (tab && tab.click) return !!tab.click(event, {act: itemAction, api: (path, opt) => getJson('/api/ext/' + rt.ext + '/' + path, opt)});
    return false;
  }
  return {route, refresh, render, click, badge, extLinks, windowLabel, needsYou};
})();
